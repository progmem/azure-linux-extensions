#!/usr/bin/env python
#
# Azure Linux extension
#
# Linux Azure Diagnostic Extension (Current version is specified in manifest.xml)
# Copyright (c) Microsoft Corporation
# All rights reserved.
# MIT License
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
#  documentation files (the ""Software""), to deal in the Software without restriction, including without limitation
#  the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
#  permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
#  Software.
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
#  WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
#  OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#  OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import os
import traceback
import xml.etree.ElementTree as ET

import Providers.Builtin as BuiltIn
import Utils.ProviderUtil as ProvUtil
import Utils.LadDiagnosticUtil as LadUtil
import Utils.XmlUtil as XmlUtil
import Utils.mdsd_xml_templates as mxt
import telegraf_utils.telegraf_config_handler as telhandler
import metrics_ext_utils.metrics_ext_handler as me_handler 
from Utils.lad_exceptions import LadLoggingConfigException, LadPerfCfgConfigException
from Utils.lad_logging_config import LadLoggingConfig, copy_source_mdsdevent_eh_url_elems
from Utils.misc_helpers import get_storage_endpoints_with_account, escape_nonalphanumerics


class LadConfigAll:
    """
    A class to generate configs for all 3 core components of LAD: mdsd, omsagent (fluentd), and syslog
    (rsyslog or syslog-ng) based on LAD's JSON extension settings.
    The mdsd XML config file generated will be /var/lib/waagent/Microsoft. ...-x.y.zzzz/xmlCfg.xml (hard-coded).
    Other config files whose contents are generated by this class are as follows:
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/syslog.conf : fluentd's syslog source config
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/tail.conf : fluentd's tail source config (fileLogs)
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/z_out_mdsd.conf : fluentd's out_mdsd out plugin config
    - /etc/rsyslog.conf or /etc/rsyslog.d/95-omsagent.conf: rsyslog config for LAD's syslog settings
       The content should be appended to the corresponding file, not overwritten. After that, the file should be
       processed so that the '%SYSLOG_PORT%' pattern is replaced with the assigned TCP port number.
    - /etc/syslog-ng.conf: syslog-ng config for LAD's syslog settings. The content should be appended, not overwritten.
    """
    _default_perf_cfgs = [
        {"query": "SELECT PercentAvailableMemory, AvailableMemory, UsedMemory, PercentUsedSwap "
                  "FROM SCX_MemoryStatisticalInformation",
         "table": "LinuxMemory"},
        {"query": "SELECT PercentProcessorTime, PercentIOWaitTime, PercentIdleTime "
                  "FROM SCX_ProcessorStatisticalInformation WHERE Name='_TOTAL'",
         "table": "LinuxCpu"},
        {"query": "SELECT AverageWriteTime,AverageReadTime,ReadBytesPerSecond,WriteBytesPerSecond "
                  "FROM  SCX_DiskDriveStatisticalInformation WHERE Name='_TOTAL'",
         "table": "LinuxDisk"}
    ]

    def __init__(self, ext_settings, ext_dir, waagent_dir, deployment_id,
                 fetch_uuid, encrypt_string, logger_log, logger_error):
        """
        Constructor.
        :param ext_settings: A LadExtSettings (in Utils/lad_ext_settings.py) obj wrapping the Json extension settings.
        :param ext_dir: Extension directory (e.g., /var/lib/waagent/Microsoft.OSTCExtensions.LinuxDiagnostic-2.3.xxxx)
        :param waagent_dir: WAAgent directory (e.g., /var/lib/waagent)
        :param deployment_id: Deployment ID string (or None) that should be obtained & passed by the caller
                              from waagent's HostingEnvironmentCfg.xml.
        :param fetch_uuid: A function which fetches the UUID for the VM
        :param encrypt_string: A function which encrypts a string, given a cert_path
        :param logger_log: Normal logging function (e.g., hutil.log) that takes only one param for the logged msg.
        :param logger_error: Error logging function (e.g., hutil.error) that takes only one param for the logged msg.
        """
        self._ext_settings = ext_settings
        self._ext_dir = ext_dir
        self._waagent_dir = waagent_dir
        self._deployment_id = deployment_id
        self._fetch_uuid = fetch_uuid
        self._encrypt_secret = encrypt_string
        self._logger_log = logger_log
        self._logger_error = logger_error
        self._telegraf_me_url = "udp://127.0.0.1:8089"
        self._telegraf_mdsd_url = "unix:///var/run/mdsd/default_influx.socket"

        # Generated logging configs place holders
        self._fluentd_syslog_src_config = None
        self._fluentd_tail_src_config = None
        self._fluentd_out_mdsd_config = None
        self._rsyslog_config = None
        self._syslog_ng_config = None
        self._telegraf_config = None
        self._telegraf_namespaces = None

        self._mdsd_config_xml_tree = ET.ElementTree(ET.fromstring(mxt.entire_xml_cfg_tmpl))
        self._sink_configs = LadUtil.SinkConfiguration()
        self._sink_configs.insert_from_config(self._ext_settings.read_protected_config('sinksConfig'))
        # If we decide to also read sinksConfig from ladCfg, do it first, so that private settings override

        # Get encryption settings
        thumbprint = ext_settings.get_handler_settings()['protectedSettingsCertThumbprint']
        self._cert_path = os.path.join(waagent_dir, thumbprint + '.crt')
        self._pkey_path = os.path.join(waagent_dir, thumbprint + '.prv')

    def _ladCfg(self):
        return self._ext_settings.read_public_config('ladCfg')

    @staticmethod
    def _wad_table_name(interval):
        """
        Build the name and storetype of a metrics table based on the aggregation interval and presence/absence of sinks 
        :param str interval: String representation of aggregation interval
        :return: table name
        :rtype: str
        """
        return 'WADMetrics{0}P10DV2S'.format(interval)

    def _add_element_from_string(self, path, xml_string, add_only_once=True):
        """
        Add an XML fragment to the mdsd config document in accordance with path
        :param str path: Where to add the fragment
        :param str xml_string: A string containing the XML element to add
        :param bool add_only_once: Indicates whether to perform the addition only to the first match of the path.
        """
        XmlUtil.addElement(xml=self._mdsd_config_xml_tree, path=path, el=ET.fromstring(xml_string),
                           addOnlyOnce=add_only_once)

    def _add_element_from_element(self, path, xml_elem, add_only_once=True):
        """
        Add an XML fragment to the mdsd config document in accordance with path
        :param str path: Where to add the fragment
        :param ElementTree xml_elem: An ElementTree object XML fragment that should be added to the path.
        :param bool add_only_once: Indicates whether to perform the addition only to the first match of the path.
        """
        XmlUtil.addElement(xml=self._mdsd_config_xml_tree, path=path, el=xml_elem, addOnlyOnce=add_only_once)

    def _add_derived_event(self, interval, source, event_name, store_type, add_lad_query=False):
        """
        Add a <DerivedEvent> element to the configuration
        :param str interval: Interval at which this DerivedEvent should be run 
        :param str source: Local table from which this DerivedEvent should pull
        :param str event_name: Destination table to which this DerivedEvent should push
        :param str store_type: The storage type of the destination table, e.g. Local, Central, JsonBlob
        :param bool add_lad_query: True if a <LadQuery> subelement should be added to this <DerivedEvent> element
        """
        derived_event = mxt.derived_event.format(interval=interval, source=source, target=event_name, type=store_type)
        element = ET.fromstring(derived_event)
        if add_lad_query:
            XmlUtil.addElement(element, ".", ET.fromstring(mxt.lad_query))
        self._add_element_from_element('Events/DerivedEvents', element)

    def _add_obo_field(self, name, value):
        """
        Add an <OboDirectPartitionField> element to the <Management> element.
        :param name: Name of the field
        :param value: Value for the field
        """
        self._add_element_from_string('Management', mxt.obo_field.format(name=name, value=value))

    def _update_metric_collection_settings(self, ladCfg):
        """
        Update mdsd_config_xml_tree for Azure Portal metric collection. The mdsdCfg performanceCounters element contains
        an array of metric definitions; this method passes each definition to its provider's AddMetric method, which is
        responsible for configuring the provider to deliver the metric to mdsd and for updating the mdsd config as
        required to expect the metric to arrive. This method also builds the necessary aggregation queries (from the
        metrics.metricAggregation array) that grind the ingested data and push it to the WADmetric table.
        :param ladCfg: ladCfg object from extension config
        :return: None
        """
        metrics = LadUtil.getPerformanceCounterCfgFromLadCfg(ladCfg)
        if not metrics:
            return

        counter_to_table = {}
        local_tables = set()

        # Add each metric
        for metric in metrics:
            if metric['type'] == 'builtin':
                local_table_name = BuiltIn.AddMetric(metric)
                if local_table_name:
                    local_tables.add(local_table_name)
                    counter_to_table[metric['counterSpecifier']] = local_table_name

        # Finalize; update the mdsd config to be prepared to receive the metrics
        BuiltIn.UpdateXML(self._mdsd_config_xml_tree)

        # Aggregation is done by <LADQuery> within a <DerivedEvent>. If there are no alternate sinks, the DerivedQuery
        # can send output directly to the WAD metrics table. If there *are* alternate sinks, have the LADQuery send
        # output to a new local table, then arrange for additional derived queries to pull from that.
        intervals = LadUtil.getAggregationPeriodsFromLadCfg(ladCfg)
        sinks = LadUtil.getFeatureWideSinksFromLadCfg(ladCfg, 'performanceCounters')
        for table_name in local_tables:
            for aggregation_interval in intervals:
                if sinks:
                    local_table_name = ProvUtil.MakeUniqueEventName('aggregationLocal')
                    self._add_derived_event(aggregation_interval, table_name,
                                            local_table_name,
                                            'Local', add_lad_query=True)
                    self._handle_alternate_sinks(aggregation_interval, sinks, local_table_name)
                else:
                    self._add_derived_event(aggregation_interval, table_name,
                                            "omi"+LadConfigAll._wad_table_name(aggregation_interval),
                                            'Central', add_lad_query=True)

    def _handle_alternate_sinks(self, interval, sinks, source):
        """
        Update the XML config to accommodate alternate data sinks. Start by pumping the data from the local source to
        the actual wad table; then run through the sinks and add annotations or additional DerivedEvents as needed.
        :param str interval: Aggregation interval
        :param [str] sinks: List of alternate destinations 
        :param str source: Name of local table from which data is to be pumped
        :return: 
        """
        self._add_derived_event(interval, source, "omi"+LadConfigAll._wad_table_name(interval), 'Central')
        for name in sinks:
            sink = self._sink_configs.get_sink_by_name(name)
            if sink is None:
                self._logger_log("Ignoring sink '{0}' for which no definition was found".format(name))
            elif sink['type'] == 'EventHub':
                if 'sasURL' in sink:
                    self._add_streaming_annotation(source, sink['sasURL'])
                else:
                    self._logger_error("Ignoring EventHub sink '{0}': no 'sasURL' was supplied".format(name))
            elif sink['type'] == 'JsonBlob':
                self._add_derived_event(interval, source, name, 'JsonBlob')
            else:
                self._logger_log("Ignoring sink '{0}': unknown type '{1}'".format(name, sink['type']))

    def _update_raw_omi_events_settings(self, omi_queries):
        """
        Update the mdsd XML tree with the OMI queries provided.
        :param omi_queries: List of dictionaries specifying OMI queries and destination tables. E.g.:
         [
             {"query":"SELECT PercentAvailableMemory, AvailableMemory, UsedMemory, PercentUsedSwap FROM SCX_MemoryStatisticalInformation","table":"LinuxMemory"},
             {"query":"SELECT PercentProcessorTime, PercentIOWaitTime, PercentIdleTime FROM SCX_ProcessorStatisticalInformation WHERE Name='_TOTAL'","table":"LinuxCpu"},
             {"query":"SELECT AverageWriteTime,AverageReadTime,ReadBytesPerSecond,WriteBytesPerSecond FROM  SCX_DiskDriveStatisticalInformation WHERE Name='_TOTAL'","table":"LinuxDisk"}
         ]
        :return: None. The mdsd XML tree member is updated accordingly.
        """
        if not omi_queries:
            return

        def generate_omi_query_xml_elem(omi_query, sink=()):
            """
            Helper for generating OMI event XML element
            :param omi_query: Python dictionary object for the raw OMI query specified as a LAD 3.0 perfCfg array item
            :param sink: (name, type) tuple for this OMI query. Not specified implies default XTable sink
            :return: An XML element object for this OMI event that should be added to the mdsd XML cfg tree
            """
            omi_xml_schema = """
            <OMIQuery cqlQuery="" dontUsePerNDayTable="true" eventName="" omiNamespace="" priority="High" sampleRateInSeconds="" storeType="" />
            """ if sink else """
            <OMIQuery cqlQuery="" dontUsePerNDayTable="true" eventName="" omiNamespace="" priority="High" sampleRateInSeconds="" />
            """
            xml_elem = XmlUtil.createElement(omi_xml_schema)
            xml_elem.set('cqlQuery', omi_query['query'])
            xml_elem.set('eventName', sink[0] if sink else omi_query['table'])
            # Default OMI namespace is 'root/scx'
            xml_elem.set('omiNamespace', omi_query['namespace'] if 'namespace' in omi_query else 'root/scx')
            # Default query frequency is 300 seconds
            xml_elem.set('sampleRateInSeconds', str(omi_query['frequency']) if 'frequency' in omi_query else '300')
            if sink:
                xml_elem.set('storeType', 'local' if sink[1] == 'EventHub' else sink[1])
            return xml_elem

        for omi_query in omi_queries:
            if ('query' not in omi_query) or ('table' not in omi_query and 'sinks' not in omi_query):
                self._logger_log("Ignoring perfCfg array element missing required elements: '{0}'".format(omi_query))
                continue
            if 'table' in omi_query:
                self._add_element_from_element('Events/OMI', generate_omi_query_xml_elem(omi_query))
            for sink_name in LadUtil.getSinkList(omi_query):
                sink = self._sink_configs.get_sink_by_name(sink_name)
                if not sink:
                    raise LadPerfCfgConfigException('Sink name "{0}" is not defined in sinksConfig'.format(sink_name))
                sink_type = sink['type']
                if sink_type != 'JsonBlob' and sink_type != 'EventHub':
                    raise LadPerfCfgConfigException('Sink type "{0}" (for sink name="{1}") is not supported'
                                                    .format(sink_type, sink_name))
                self._add_element_from_element('Events/OMI', generate_omi_query_xml_elem(omi_query, (sink_name, sink_type)))
                if sink_type == 'EventHub':
                    if 'sasURL' not in sink:
                        raise LadPerfCfgConfigException('No sasURL specified for an EventHub sink (name="{0}")'
                                                        .format(sink_name))
                    self._add_streaming_annotation(sink_name, sink['sasURL'])

    def _add_streaming_annotation(self, sink_name, sas_url):
        """
        Helper to add an EventStreamingAnnotation element for the given sink_name and sas_url
        :param str sink_name: Name of the EventHub sink name for the SAS URL
        :param str sas_url: Raw SAS URL string for the EventHub sink
        """
        self._add_element_from_string('EventStreamingAnnotations',
                                      mxt.per_eh_url_tmpl.format(eh_name=sink_name,
                                                                 key_path=self._pkey_path,
                                                                 enc_eh_url=self._encrypt_secret_with_cert(sas_url)))

    def _apply_perf_cfg(self):
        """
        Extract the 'perfCfg' settings from ext_settings and apply them to mdsd config XML root. These are *not* the
        ladcfg{performanceCounters{...}} settings; the perfCfg block is found at the top level of the public configs.
        :return: None. Changes are applied directly to the mdsd config XML tree member.
        """
        assert self._mdsd_config_xml_tree is not None

        perf_cfg = self._ext_settings.read_public_config('perfCfg')
        self._update_raw_omi_events_settings(perf_cfg)

    def _encrypt_secret_with_cert(self, secret):
        """
        update_account_settings() helper.
        :param secret: Secret to encrypt
        :return: Encrypted secret string. None if openssl command exec fails.
        """
        return self._encrypt_secret(self._cert_path, secret)

    def _update_account_settings(self, account, token, endpoints):
        """
        Update the MDSD configuration Account element with Azure table storage properties.
        Exactly one of (key, token) must be provided.
        :param account: Storage account to which LAD should write data
        :param token: SAS token to access the storage account
        :param endpoints: Identifies the Azure storage endpoints (public or specific sovereign cloud) where the storage account is
        """
        assert token, "Token must be given."
        assert self._mdsd_config_xml_tree is not None

        token = self._encrypt_secret_with_cert(token)
        assert token, "Could not encrypt token"
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                            "account", account, ['isDefault', 'true'])
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                            "key", token, ['isDefault', 'true'])
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                            "decryptKeyPath", self._pkey_path, ['isDefault', 'true'])
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                            "tableEndpoint", endpoints[0], ['isDefault', 'true'])
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                            "blobEndpoint", endpoints[1], ['isDefault', 'true'])
        XmlUtil.removeElement(self._mdsd_config_xml_tree, 'Accounts', 'Account')

    def _set_xml_attr(self, key, value, xml_path, selector=[]):
        """
        Set XML attribute on the element specified with xml_path.
        :param key: The attribute name to set on the XML element.
        :param value: The default value to be set, if there's no public config for that attribute.
        :param xml_path: The path of the XML element(s) to which the attribute is applied.
        :param selector: Selector for finding the actual XML element (see XmlUtil.setXmlValue)
        :return: None. Change is directly applied to mdsd_config_xml_tree XML member object.
        """
        assert self._mdsd_config_xml_tree is not None

        v = self._ext_settings.read_public_config(key)
        if not v:
            v = value
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, xml_path, key, v, selector)

    def _set_event_volume(self, lad_cfg):
        """
        Set event volume in mdsd config. Check if desired event volume is specified,
        first in ladCfg then in public config. If in neither then default to Medium.
        :param lad_cfg: 'ladCfg' Json object to look up for the event volume setting.
        :return: None. The mdsd config XML tree's eventVolume attribute is directly updated.
        :rtype: str
        """
        assert self._mdsd_config_xml_tree is not None

        event_volume = LadUtil.getEventVolumeFromLadCfg(lad_cfg)
        if event_volume:
            self._logger_log("Event volume found in ladCfg: " + event_volume)
        else:
            event_volume = self._ext_settings.read_public_config("eventVolume")
            if event_volume:
                self._logger_log("Event volume found in public config: " + event_volume)
            else:
                event_volume = "Medium"
                self._logger_log("Event volume not found in config. Using default value: " + event_volume)
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, "Management", "eventVolume", event_volume)

    ######################################################################
    # This is the main API that's called by user. All other methods are
    # actually helpers for this, thus made private by convention.
    ######################################################################
    def generate_all_configs(self):
        """
        Generates configs for all components required by LAD.
        Generates XML cfg file for mdsd, from JSON config settings (public & private).
        Also generates rsyslog/syslog-ng configs corresponding to 'syslogEvents' or 'syslogCfg' setting.
        Also generates fluentd's syslog/tail src configs and out_mdsd configs.
        The rsyslog/syslog-ng and fluentd configs are not yet saved to files. They are available through
        the corresponding getter methods of this class (get_fluentd_*_config(), get_*syslog*_config()).

        Returns (True, '') if config was valid and proper xmlCfg.xml was generated.
        Returns (False, '...') if config was invalid and the error message.
        """

        # 1. Add DeploymentId (if available) to identity columns
        if self._deployment_id:
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, "Management/Identity/IdentityComponent", "",
                                self._deployment_id, ["name", "DeploymentId"])
        # 2. Use ladCfg to generate OMIQuery and LADQuery elements
        lad_cfg = self._ladCfg()
        if lad_cfg:
            try:
                self._update_metric_collection_settings(lad_cfg)
                resource_id = self._ext_settings.get_resource_id()
                if resource_id:
                    XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Events/DerivedEvents/DerivedEvent/LADQuery',
                                        'partitionKey', escape_nonalphanumerics(resource_id))
                    lad_query_instance_id = ""
                    uuid_for_instance_id = self._fetch_uuid()
                    if resource_id.find("providers/Microsoft.Compute/virtualMachineScaleSets") >= 0:
                        lad_query_instance_id = uuid_for_instance_id
                    self._set_xml_attr("instanceID", lad_query_instance_id, "Events/DerivedEvents/DerivedEvent/LADQuery")
                    # Set JsonBlob sink-related elements
                    self._add_obo_field(name='resourceId', value=resource_id)
                    self._add_obo_field(name='agentIdentityHash', value=uuid_for_instance_id)

            except Exception as e:
                self._logger_error("Failed to create portal config  error:{0} {1}".format(e, traceback.format_exc()))
                return False, 'Failed to create portal config from ladCfg (see extension error logs for more details)'

        # 3. Generate config for perfCfg. Need to distinguish between non-AppInsights scenario and AppInsights scenario,
        #    so check if Application Insights key is present and pass it to the actual helper
        #    function (self._apply_perf_cfg()).
        try:
            self._apply_perf_cfg()
        except Exception as e:
            self._logger_error("Failed check for Application Insights key in LAD configuration with exception:{0}\n"
                               "Stacktrace: {1}".format(e, traceback.format_exc()))
            return False, 'Failed to update perf counter config (see extension error logs for more details)'

        # 4. Generate omsagent (fluentd) configs, rsyslog/syslog-ng config, and update corresponding mdsd config XML
        try:
            syslogEvents_setting = self._ext_settings.get_syslogEvents_setting()
            fileLogs_setting = self._ext_settings.get_fileLogs_setting()
            perf_settings = LadUtil.getDiagnosticsMonitorConfigurationElement(self._ext_settings.read_public_config('ladCfg'), 'performanceCounters')

            lad_logging_config_helper = LadLoggingConfig(syslogEvents_setting, fileLogs_setting, self._sink_configs,
                                                         self._pkey_path, self._cert_path, self._encrypt_secret)
            mdsd_syslog_config = lad_logging_config_helper.get_mdsd_syslog_config()
            mdsd_filelog_config = lad_logging_config_helper.get_mdsd_filelog_config()
            copy_source_mdsdevent_eh_url_elems(self._mdsd_config_xml_tree, mdsd_syslog_config)
            copy_source_mdsdevent_eh_url_elems(self._mdsd_config_xml_tree, mdsd_filelog_config)
            self._fluentd_syslog_src_config = lad_logging_config_helper.get_fluentd_syslog_src_config()
            self._fluentd_tail_src_config = lad_logging_config_helper.get_fluentd_filelog_src_config()
            self._fluentd_out_mdsd_config = lad_logging_config_helper.get_fluentd_out_mdsd_config()
            self._rsyslog_config = lad_logging_config_helper.get_rsyslog_config()
            self._syslog_ng_config = lad_logging_config_helper.get_syslog_ng_config()
            parsed_perf_settings = lad_logging_config_helper.parse_lad_perf_settings(perf_settings)
            self._telegraf_config, self._telegraf_namespaces = telhandler.handle_config(parsed_perf_settings, self._telegraf_me_url, self._telegraf_mdsd_url, True) 
            mdsd_telegraf_config = lad_logging_config_helper.get_mdsd_telegraf_config(self._telegraf_namespaces, LadConfigAll._wad_table_name("PT1H"))
            copy_source_mdsdevent_eh_url_elems(self._mdsd_config_xml_tree, mdsd_telegraf_config)
            me_handler.setup_me(True)

        except Exception as e:
            self._logger_error("Failed to create omsagent (fluentd), rsyslog/syslog-ng configs, telegraf config or to update "
                               "corresponding mdsd config XML. Error: {0}\nStacktrace: {1}"
                               .format(e, traceback.format_exc()))
            return False, 'Failed to generate configs for fluentd, syslog, and mdsd ' \
                          '(see extension error logs for more details)'

        # 5. Before starting to update the storage account settings, log extension's entire settings
        #    with secrets redacted, for diagnostic purpose.
        self._ext_settings.log_ext_settings_with_secrets_redacted(self._logger_log, self._logger_error)

        # 6. Actually update the storage account settings on mdsd config XML tree (based on extension's
        #    protectedSettings).
        account = self._ext_settings.read_protected_config('storageAccountName').strip()
        if not account:
            return False, "Must specify storageAccountName"
        if self._ext_settings.read_protected_config('storageAccountKey'):
            return False, "The storageAccountKey protected setting is not supported and must not be used"
        token = self._ext_settings.read_protected_config('storageAccountSasToken').strip()
        if not token or token == '?':
            return False, "Must specify storageAccountSasToken"
        if '?' == token[0]:
            token = token[1:]
        endpoints = get_storage_endpoints_with_account(account,
                                                     self._ext_settings.read_protected_config('storageAccountEndPoint'))
        self._update_account_settings(account, token, endpoints)

        # 7. Update mdsd config XML's eventVolume attribute based on the logic specified in the helper.
        self._set_event_volume(lad_cfg)

        # 8. Finally generate mdsd config XML file out of the constructed XML tree object.
        self._mdsd_config_xml_tree.write(os.path.join(self._ext_dir, 'xmlCfg.xml'))
        
        return True, ""

    @staticmethod
    def __throw_if_output_is_none(output):
        """
        Helper to check if output is already generated (not None) and throw if it's not (None).
        :return: None
        """
        if output is None:
            raise LadLoggingConfigException('LadConfigAll.get_*_config() should be called after '
                                            'LadConfigAll.generate_mdsd_omsagent_syslog_config() is called')

    def get_fluentd_syslog_src_config(self):
        """
        Returns the obtained Fluentd's syslog src config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/syslog.conf
        after replacing '%SYSLOG_PORT%' with the assigned TCP port number.
        :rtype: str
        :return: Fluentd syslog src config string
        """
        LadConfigAll.__throw_if_output_is_none(self._fluentd_syslog_src_config)
        return self._fluentd_syslog_src_config

    def get_fluentd_tail_src_config(self):
        """
        Returns the obtained Fluentd's tail src config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/tail.conf.
        :rtype: str
        :return: Fluentd tail src config string
        """
        LadConfigAll.__throw_if_output_is_none(self._fluentd_tail_src_config)
        return self._fluentd_tail_src_config

    def get_fluentd_out_mdsd_config(self):
        """_fluentd_out_mdsd_config
        Returns the obtained Fluentd's out_mdsd config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/z_out_mdsd.conf.
        :rtype: str
        :return: Fluentd out_mdsd config string
        """
        LadConfigAll.__throw_if_output_is_none(self._fluentd_out_mdsd_config)
        return self._fluentd_out_mdsd_config

    def get_rsyslog_config(self):
        """
        Returns the obtained rsyslog config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be appended to /etc/rsyslog.d/95-omsagent.conf if rsyslog ver is new (that is, if
        /etc/rsyslog.d/ exists). It should be appended to /etc/rsyslog.conf if rsyslog ver is old (no /etc/rsyslog.d/).
        The appended file (either /etc/rsyslog.d/95-omsagent.conf or /etc/rsyslog.conf) should be processed so that
        the '%SYSLOG_PORT%' pattern in the file is replaced with the assigned TCP port number.
        :rtype: str
        :return: rsyslog config string
        """
        LadConfigAll.__throw_if_output_is_none(self._rsyslog_config)
        return self._rsyslog_config

    def get_syslog_ng_config(self):
        """
        Returns the obtained syslog-ng config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be appended to /etc/syslog-ng.conf.
        The appended file (/etc/syslog-ng.conf) should be processed so that
        the '%SYSLOG_PORT%' pattern in the file is replaced with the assigned TCP port number.
        :rtype: str
        :return: syslog-ng config string
        """
        LadConfigAll.__throw_if_output_is_none(self._syslog_ng_config)
        return self._syslog_ng_config

