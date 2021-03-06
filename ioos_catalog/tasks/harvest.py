from bson import ObjectId
from datetime import datetime
from lxml import etree
import itertools
import re
import requests
import math
from urllib2 import HTTPError
# py2/3 compat
from six.moves.urllib.request import urlopen

from owslib import ows
from owslib.sos import SensorObservationService
from owslib.swe.sensor.sml import SensorML
from owslib.util import testXMLAttribute, testXMLValue
from owslib.crs import Crs

from pyoos.parsers.ioos.describe_sensor import IoosDescribeSensor
from paegan.cdm.dataset import CommonDataset, _possiblet, _possiblez, _possiblex, _possibley
from petulantbear.netcdf2ncml import *
from petulantbear.netcdf_etree import parse_nc_dataset_as_etree
from petulantbear.netcdf_etree import namespaces as pb_namespaces
from netCDF4 import Dataset
import numpy as np

from compliance_checker.runner import ComplianceCheckerCheckSuite
from compliance_checker.ioos import IOOSSOSGCCheck, IOOSSOSDSCheck, IOOSNCCheck
from compliance_checker.base import get_namespaces
from wicken.xml_dogma import MultipleXmlDogma
from wicken.netcdf_dogma import NetCDFDogma

from shapely.geometry import mapping, box, Point, asLineString

import geojson
import json

from ioos_catalog import app, db, queue
from ioos_catalog.tasks.send_email import send_service_down_email
from ioos_catalog.tasks.debug import debug_wrapper, breakpoint
#from ioos_catalog.models import MetricCount
from functools import wraps
from datetime import datetime
# mainly for conversion from np.datetime64 -> datetime.datetime
from pandas import Timestamp
from dateutil.parser import parse
from netCDF4 import num2date

LARGER_SERVICES = [
    ObjectId('53d34aed8c0db37e0b538fda'),
    ObjectId('53d49c8d8c0db37ff1370308')
]

def queue_harvest_tasks():
    """
    Generate a number of harvest tasks.

    Meant to be called via cron. Only queues services that are active.
    """

    with app.app_context():
        for s in db.Service.find({'active':True}, {'_id':True}):
            service_id = s._id
            if service_id in LARGER_SERVICES:
                continue
            # count all the datasets associated with this particular service
            datalen = db.datasets.find({'services.service_id':
                                         service_id}).count()
            # handle timeouts for services with large numbers of datasets
            if datalen <= 36:
                timeout_secs = 180
            else:
                # for large numbers of requests, 5 seconds should be enough
                # for each request, on average
                timeout_secs = datalen * 60
            queue.enqueue_call(harvest, args=(service_id,),
                               timeout=timeout_secs)


    # record dataset/service metrics after harvest
    add_counts()

def queue_provider(provider):
    with app.app_context():
        for s in db.Service.find({'data_provider':provider, 'active':True}):
            service_id = s._id
            if service_id in LARGER_SERVICES:
                continue
            # count all the datasets associated with this particular service
            datalen = db.datasets.find({'services.service_id':
                                         service_id}).count()
            # handle timeouts for services with large numbers of datasets
            if datalen <= 36:
                timeout_secs = 180
            else:
                # for large numbers of requests, 5 seconds should be enough
                # for each request, on average
                timeout_secs = datalen * 60
            queue.enqueue_call(harvest, args=(service_id,),
                               timeout=timeout_secs)

    # record dataset/service metrics after harvest
    add_counts()


def queue_large_service_harvest_tasks():
    larger_services = [
        ObjectId('53d34aed8c0db37e0b538fda'),
        ObjectId('53d49c8d8c0db37ff1370308')
    ]
    with app.app_context():
        for s in db.Service.find({'_id':{'$in':larger_services}}):
            service_id = s._id
            # count all the datasets associated with this particular service
            datalen = db.datasets.find({'services.service_id':
                                         service_id}).count()
            # handle timeouts for services with large numbers of datasets
            if datalen <= 36:
                timeout_secs = 180
            else:
                # for large numbers of requests, 5 seconds should be enough
                # for each request, on average
                timeout_secs = datalen * 60
            queue.enqueue_call(harvest, args=(service_id,),
                               timeout=timeout_secs)

    # record dataset/service metrics after harvest
    add_counts()


# TODO: Roll into respective model methods instead
def add_counts():
    """Returns a timestamped aggregated count"""
    collection = db.metric_counts
    services_pipeline_ra = [{'$group': {'_id': '$data_provider',
                          "count": {"$sum": 1},
                          "active_count": {"$sum": {"$cond": ["$active", 1, 0]}}}},
                          {"$project": {"_id": 1, "count": 1, "active_count": 1,
                           "inactive_count": {"$subtract":
                                                ["$count", "$active_count"]}}}]
    services_arr = db.services.aggregate(services_pipeline_ra)['result']

    services_by_ra = collection.MetricCount({'date': datetime.utcnow(),
                                            'stats_type': u'services_by_ra',
                                            'count': services_arr})
    services_by_ra.save()


    services_pipeline_type = [{'$group': {'_id': '$service_type',
                          "count": {"$sum": 1},
                          "active_count": {"$sum": {"$cond": ["$active", 1, 0]}}}},
                          {"$project": {"_id": 1, "count": 1, "active_count": 1,
                           "inactive_count": {"$subtract":
                                                ["$count", "$active_count"]}}}]

    services_arr_type = db.services.aggregate(services_pipeline_type)['result']

    services_by_type = collection.MetricCount({'date': datetime.utcnow(),
                                               'stats_type':
                                               u'services_by_type',
                                               'count': services_arr_type})
    services_by_type.save()

    # get the total, active, inactive counts per RA for datasets by getting
    # unique data providers in the services array
    # When a dataset is shared between services, count once for each data
    # provider
    datasets_pipeline = [{"$unwind": "$services"},
                         {"$project":
                            {"data_provider": '$services.data_provider',
                             "active": "$active"}},
                         {"$group": { "_id": {"_id": "$_id",
                                              "data_provider": "$data_provider",
                                              "active": "$active"}}},
                         {"$project": {"_id": "$_id._id",
                                       "data_provider": "$_id.data_provider",
                                       "active": "$_id.active"}},
                         {"$group": {"_id": "$data_provider",
                                     "total_services": {"$sum": 1},
                                     "active_services": { "$sum":
                                            {"$cond": ["$active", 1, 0]}} }},
                         {"$project": {"_id": 1, "total_services": 1,
                                       "active_services": 1,
                                       "inactive_services": {"$subtract":
                                                             ["$total_services",
                                                              "$active_services"
                                                             ]}
                                        }
                         }]

    datasets_arr = db.datasets.aggregate(datasets_pipeline)['result']

    datasets_by_ra = collection.MetricCount({'date': datetime.utcnow(),
                                             'stats_type': u'datasets_by_ra',
                                             'count': datasets_arr})
    datasets_by_ra.save()

def context_decorator(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        with app.app_context():
            return f(*args, **kwargs)
    return wrapper

@debug_wrapper
@context_decorator
def harvest(service_id, ignore_active=False):

    # Get the harvest or make a new one
    harvest = db.Harvest.find_one( { 'service_id' : ObjectId(service_id) } )
    if harvest is None:
        harvest = db.Harvest()
        harvest.service_id = ObjectId(service_id)

    harvest.harvest(ignore_active=ignore_active)
    harvest.save()
    return harvest.harvest_status



def unicode_or_none(thing):
    try:
        if thing is None:
            return thing
        else:
            try:
                return unicode(thing)
            except:
                return None
    except:
        return None


def get_common_name(data_type):
    """Map names from various standards to return a human readable form"""
    # TODO: should probably split this into DAP and SOS specific mappings
    mapping_dict = {
        # Remap UNKNOWN, None to Unspecified
        None: 'Unspecified',
        'UNKNOWN': 'Unspecified',
        '(NONE)': 'Unspecified',
        # Rectangular grids remap to the CF feature type "grid"
        'grid': 'Regular Grid',
        'Grid': 'Regular Grid',
        'GRID': 'Regular Grid',
        'RGRID': 'Regular Grid',
        # Curvilinear grids
        'CGRID': 'Curvilinear Grid',
        # remap some CDM `cdm_data_type`s to equivalent CF-1.6 `featureType`s
        'trajectory': 'Trajectory',
        'point': 'Point',
        # UGrid to unstructured grid
        'ugrid': 'Unstructured Grid',
        # Buoys
        'BUOY': 'Buoy',
        # time series
        'timeSeries': 'Time Series'
    }

    # Get the common name if defined, otherwise return initial value
    return unicode(mapping_dict.get(data_type, data_type))



class Harvester(object):
    def __init__(self, service):
        self.service = service

    @context_decorator
    def save_ccheck_and_metadata(self, service_id, checker_name, ref_id, ref_type, scores, metamap):
        """
        Saves the result of a compliance checker scores and metamap document.

        Will be called by service/station derived methods.
        """
        if not (scores or metamap):
            return

        def res2dict(r):
            cl = []
            if getattr(r, 'children', None):
                cl = map(res2dict, r.children)

            return {'name'     : unicode(r.name),
                    'score'    : float(r.value[0]),
                    'maxscore' : float(r.value[1]),
                    'weight'   : int(r.weight),
                    'children' : cl}

        metadata = db.Metadata.find_one({'ref_id': ref_id})
        if metadata is None:
            metadata             = db.Metadata()
            metadata.ref_id      = ref_id
            metadata.ref_type    = unicode(ref_type)

        if isinstance(scores, tuple): # New API of compliance-checker
            scores = scores[0]
        cc_results = map(res2dict, scores)

        # @TODO: srsly need to decouple from cchecker
        score     = sum(((float(r.value[0])/r.value[1]) * r.weight for r in scores))
        max_score = sum((r.weight for r in scores))

        score_doc = {'score'     : float(score),
                     'max_score' : float(max_score),
                     'pct'       : float(score) / max_score}

        update_doc = {'cc_score'   : score_doc,
                      'cc_results' : cc_results,
                      'metamap'    : metamap}

        for mr in metadata.metadata:
            if mr['service_id'] == service_id and mr['checker'] == checker_name:
                mr.update(update_doc)
                break
        else:
            metarecord = {'service_id': service_id,
                          'checker'   : unicode(checker_name)}
            metarecord.update(update_doc)
            metadata.metadata.append(metarecord)

        metadata.updated = datetime.utcnow()
        metadata.save()

        return metadata

class SosHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)

    def _handle_ows_exception(self, **kwargs):
        try:
            return self.sos.describe_sensor(**kwargs)
        except ows.ExceptionReport as e:
            if e.code == 'InvalidParameterValue':
                # TODO: use SOS getCaps to determine valid formats
                # some only work with plain SensorML as the format

                # see if O&M will work instead
                try:
                    kwargs['outputFormat'] = 'text/xml;subtype="om/1.0.0/profiles/ioos_sos/1.0"'
                    return self.sos.describe_sensor(**kwargs)

                # see if plain sensorml wll work
                except ows.ExceptionReport as e:
                    # if this fails, just raise the exception without handling
                    # here
                    kwargs['outputFormat'] = 'text/xml;subtype="sensorML/1.0.1"'
                    return self.sos.describe_sensor(**kwargs)
            elif e.msg == 'No data found for this station':
                raise e

    def _describe_sensor(self, uid, timeout=120,
                         outputFormat='text/xml;subtype="sensorML/1.0.1/profiles/ioos_sos/1.0"'):
        """
        Issues a DescribeSensor request with fallback behavior for oddly-acting SOS servers.
        """
        kwargs = {
                    'outputFormat': outputFormat,
                    'procedure': uid,
                    'timeout': timeout
                 }

        return self._handle_ows_exception(**kwargs)


    def harvest(self):
        self.sos = SensorObservationService(self.service.get('url'))

        scores   = self.ccheck_service()
        metamap  = self.metamap_service()
        try:
            self.save_ccheck_service('ioos', scores, metamap)
        finally:
        #except Exception as e:
            #app.logger.warn("could not save compliancecheck/metamap information: %s", e)
            pass

        # List storing the stations that have already been processed in this SOS server.
        # This is kept and checked later to avoid servers that have the same stations in many offerings.
        processed = []

        # handle network:all by increasing max timeout
        net_len = len(self.sos.offerings)
        net_timeout = 120 if net_len <= 36 else 5 * net_len

        # allow searching child offerings for by name for network offerings
        name_lookup = {o.name: o for o in self.sos.offerings}
        for offering in self.sos.offerings:
            # TODO: We assume an offering should only have one procedure here
            # which will be the case in sos 2.0, but may not be the case right now
            # on some non IOOS supported servers.
            uid = offering.procedures[0]
            sp_uid = uid.split(":")

            # template:   urn:ioos:type:authority:id
            # sample:     ioos:station:wmo:21414
            if len(sp_uid) > 2 and sp_uid[2] == "network": # Network Offering
                if uid[-3:].lower() == 'all':
                    continue # Skip the all
                net = self._describe_sensor(uid, timeout=net_timeout)

                network_ds = IoosDescribeSensor(net)
                # Iterate over stations in the network and process them individually

                for proc in network_ds.procedures:

                    if proc is not None and proc.split(":")[2] == "station":
                        if not proc in processed:
                            # offering associated with this procedure
                            proc_off = name_lookup.get(proc)
                            self.process_station(proc, proc_off)
                        processed.append(proc)
            else:
                # Station Offering, or malformed urn - try it anyway as if it is a station
                if not uid in processed:
                    self.process_station(uid, offering)
                processed.append(uid)



    def process_station(self, uid, offering):
        """ Makes a DescribeSensor request based on a 'uid' parameter being a
            station procedure.  Also pass along an offering with
            getCapabilities information for items such as temporal extent"""

        GML_NS   = "http://www.opengis.net/gml"
        XLINK_NS = "http://www.w3.org/1999/xlink"

        with app.app_context():

            app.logger.info("process_station: %s", uid)
            desc_sens = self._describe_sensor(uid, timeout=1200)
            # FIXME: add some kind of notice saying the station failed
            if desc_sens is None:
                app.logger.warn("Could not get a valid describeSensor response")
                return
            metadata_value = etree.fromstring(desc_sens)
            sensor_ml = SensorML(metadata_value)
            try:
                station_ds = IoosDescribeSensor(metadata_value)
            # if this doesn't conform to IOOS SensorML sub, fall back to
            # manually picking apart the SensorML
            except ows.ExceptionReport:
                station_ds = process_sensorml(sensor_ml.members[0])

            unique_id = station_ds.id
            if unique_id is None:
                app.logger.warn("Could not get a 'stationID' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/stationID'")
                return

            dataset = db.Dataset.find_one( { 'uid' : unicode(unique_id) } )
            if dataset is None:
                dataset = db.Dataset()
                dataset.uid = unicode(unique_id)
                dataset['active'] = True

            # Find service reference in Dataset.services and remove (to replace it)
            tmp = dataset.services[:]
            for d in tmp:
                if d['service_id'] == self.service.get('_id'):
                    dataset.services.remove(d)

            # Parsing messages
            messages = []

            # NAME
            name = unicode_or_none(station_ds.shortName)
            if name is None:
                messages.append(u"Could not get a 'shortName' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/shortName'")

            # DESCRIPTION
            description = unicode_or_none(station_ds.longName)
            if description is None:
                messages.append(u"Could not get a 'longName' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/longName'")

            # PLATFORM TYPE
            asset_type = unicode_or_none(getattr(station_ds,
                                                 'platformType', None))
            if asset_type is None:
                messages.append(u"Could not get a 'platformType' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/platformType'")

            # LOCATION is in GML
            gj = None
            loc = station_ds.location
            if loc is not None and loc.tag == "{%s}Point" % GML_NS:
                pos_element = loc.find("{%s}pos" % GML_NS)
                # some older responses may uses the deprecated coordinates
                # element
                if pos_element is None:
                    # if pos not found use deprecated coordinates element
                    pos_element = loc.find("{%s}coordinates" % GML_NS)
                # strip out points
                positions = map(float, pos_element.text.split(" "))

                for el in [pos_element, loc]:
                    srs_name = testXMLAttribute(el, "srsName")
                    if srs_name:
                        crs = Crs(srs_name)
                        if crs.axisorder == "yx":
                            gj = json.loads(geojson.dumps(geojson.Point([positions[1], positions[0]])))
                        else:
                            gj = json.loads(geojson.dumps(geojson.Point([positions[0], positions[1]])))
                        break
                else:
                    if positions:
                        messages.append(u"Position(s) found but could not parse SRS: %s, %s" % (positions, srs_name))

            else:
                messages.append(u"Found an unrecognized child of the sml:location element and did not attempt to process it: %s" % loc)

            meta_str = unicode(etree.tostring(metadata_value)).strip()
            if len(meta_str) > 4000000:
                messages.append(u'Metadata document was too large to store (len: %s)' % len(meta_str))
                meta_str = u''

            service = {
                # Reset service
                'name'              : name,
                'description'       : description,
                'service_type'      : self.service.get('service_type'),
                'service_id'        : ObjectId(self.service.get('_id')),
                'data_provider'     : self.service.get('data_provider'),
                'metadata_type'     : u'sensorml',
                'metadata_value'    : u'',
                'time_min': getattr(offering, 'begin_position', None),
                'time_max': getattr(offering, 'end_position', None),
                'messages'          : map(unicode, messages),
                'keywords'          : map(unicode, sorted(station_ds.keywords)),
                'variables'         : map(unicode, sorted(station_ds.variables)),
                'asset_type'        : get_common_name(asset_type),
                'geojson'           : gj,
                'updated'           : datetime.utcnow()
            }

            dataset.services.append(service)
            dataset.updated = datetime.utcnow()
            dataset.save()

            # do compliance checker / metadata now
            scores = self.ccheck_station(sensor_ml)
            metamap = self.metamap_station(sensor_ml)

            try:
                self.save_ccheck_station('ioos', dataset._id, scores, metamap)
            except Exception as e:
                app.logger.warn("could not save compliancecheck/metamap information: %s", e)

            return "Harvest Successful"

    def ccheck_service(self):
        assert self.sos

        with app.app_context():

            scores = None

            try:
                cs = ComplianceCheckerCheckSuite()
                groups = cs.run(self.sos, 'ioos')
                scores = groups['ioos']
            except Exception as e:
                app.logger.warn("Caught exception doing Compliance Checker on SOS service: %s", e)

            return scores

    def metamap_service(self):
        assert self.sos

        with app.app_context():
            # gets a metamap document of this service using wicken
            beliefs = IOOSSOSGCCheck.beliefs()
            doc = MultipleXmlDogma('sos-gc', beliefs, self.sos._capabilities, namespaces=get_namespaces())

            # now make a map out of this
            # @TODO wicken should make this easier
            metamap = {}
            for k in beliefs:
                try:
                    metamap[k] = getattr(doc, doc._fixup_belief(k)[0])
                except Exception as e:
                    pass

            return metamap

    def save_ccheck_service(self, checker_name, scores, metamap):
        """
        Saves the result of ccheck_service and metamap
        """
        return self.save_ccheck_and_metadata(self.service._id,
                                             checker_name,
                                             self.service._id,
                                             u'service',
                                             scores,
                                             metamap)

    def ccheck_station(self, sensor_ml):
        with app.app_context():
            scores = None
            try:
                cs = ComplianceCheckerCheckSuite()
                groups = cs.run(sensor_ml, 'ioos')
                scores = groups['ioos']
            except Exception as e:
                app.logger.warn("Caught exception doing Compliance Checker on SOS station: %s", e)

            return scores

    def metamap_station(self, sensor_ml):
        with app.app_context():
            # gets a metamap document of this service using wicken
            beliefs = IOOSSOSDSCheck.beliefs()
            doc = MultipleXmlDogma('sos-ds', beliefs, sensor_ml._root, namespaces=get_namespaces())

            # now make a map out of this
            # @TODO wicken should make this easier
            metamap = {}
            for k in beliefs:
                try:
                    metamap[k] = getattr(doc, doc._fixup_belief(k)[0])
                except Exception as e:
                    pass

            return metamap

    def save_ccheck_station(self, checker_name, dataset_id, scores, metamap):
        """
        Saves the result of ccheck_station and metamap
        """
        return self.save_ccheck_and_metadata(self.service._id,
                                             checker_name,
                                             dataset_id,
                                             u'dataset',
                                             scores,
                                             metamap)

class WmsHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)
    def harvest(self):
        pass

class WcsHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)
    def harvest(self):
        pass

class DapHarvest(Harvester):

    METADATA_VAR_NAMES   = [u'crs',
                            u'projection']

    # CF standard names for Axis
    STD_AXIS_NAMES       = [u'latitude',
                            u'longitude',
                            u'time',
                            u'forecast_reference_time',
                            u'forecast_period',
                            u'ocean_sigma',
                            u'ocean_s_coordinate_g1',
                            u'ocean_s_coordinate_g2',
                            u'ocean_s_coordinate',
                            u'ocean_double_sigma',
                            u'ocean_sigma_over_z',
                            u'projection_y_coordinate',
                            u'projection_x_coordinate']

    # Some datasets don't define standard_names on axis variables.  This is used to weed them out based on the
    # actual variable name
    COMMON_AXIS_NAMES    = [u'x',
                            u'y',
                            u'lat',
                            u'latitude',
                            u'lon',
                            u'longitude',
                            u'time',
                            u'time_run',
                            u'time_offset',
                            u'ntimes',
                            u'lat_u',
                            u'lon_u',
                            u'lat_v',
                            u'lon_v  ',
                            u'lat_rho',
                            u'lon_rho',
                            u'lat_psi']

    def __init__(self, service):
        Harvester.__init__(self, service)

    @classmethod
    def get_standard_variables(cls, dataset):
        for d in dataset.variables:
            try:
                yield unicode(dataset.variables[d].getncattr("standard_name"))
            except AttributeError:
                pass


    @classmethod
    def get_asset_type(cls, cd):
        """Takes a Paegan object and returns the CF feature type
            if defined, falling back to `cdm_data_type`,
            and finally to Paegan's representation if nothing else is found"""
        #TODO: Add check for adherence to CF conventions, others (ugrid)
        nc_obj = cd.nc
        if hasattr(nc_obj, 'featureType'):
            geom_type = nc_obj.featureType
        elif hasattr(nc_obj, 'cdm_data_type'):
            geom_type = nc_obj.cdm_data_type
        else:
            geom_type = cd._datasettype.upper()
        return unicode(geom_type)


    @classmethod
    def get_axis_variables(cls, dataset):
        """
        Try to find x/y axes based on variable attributes, and return
        them in a dict
        """
        axisVars = {}
        # beware of datasets with duplicate axes!  This will continue
        for var_name, var in dataset.variables.iteritems():
            if hasattr(var, 'axis'):
                if var.axis == 'X':
                    axisVars['xname'] = var_name
                elif var.axis == 'Y':
                    axisVars['yname'] = var_name
            elif hasattr(var, '_CoordinateAxisType'):
                if var._CoordinateAxisType == 'Lon':
                    axisVars['xname'] = var_name
                elif var._CoordinateAxisType == 'Lat':
                    axisVars['yname'] = var_name
        return axisVars

    def erddap_geojson_url(self, coord_names):
        """Return geojson from a tabledap ERDDAP endpoint"""
        # truncate "s."
        x_name_trunc = coord_names['xname'][2:]
        y_name_trunc = coord_names['yname'][2:]
        gj_url = (self.service.get('url') + '.geoJson?' +
                  x_name_trunc + ',' + y_name_trunc)
        url_res = urlopen(gj_url)
        gj = json.load(url_res)
        url_res.close()
        return gj


    @classmethod
    def get_time_from_dim(cls, time_var):
        """Get min/max from a NetCDF time variable and convert to datetime"""
        ndim = len(time_var.shape)
        if ndim == 0:
            ret_val = time_var.item()
            res = ret_val, ret_val
        elif ndim == 1:
            # NetCDF Users' Guide states that when time is a coordinate variable,
            # it should be monotonically increasing or decreasing with no
            # repeated variables. Therefore, first and last elements for a
            # vector should correspond to start and end time or end and start
            # time respectively. See Section 2.3.1 of the NUG
            res = time_var[0], time_var[-1]
        else:
            # FIXME: handle multidimensional time variables.  Perhaps
            # take the first and last element of time variable in the first
            # dimension and then take the min and max of the resulting values
            return None, None

        # if not > 1d, return the min and max elements found
        min_elem, max_elem = np.min(res), np.max(res)
        if hasattr(time_var, 'calendar'):
            num2date([min_elem, max_elem], time_var.units,
                      time_var.calendar)
            return num2date([min_elem, max_elem], time_var.units,
                            time_var.calendar)
        else:
            return num2date([min_elem, max_elem], time_var.units)



    def get_min_max_time(self, cd):
        """
           Attempt to naively find a time variable in the dataset
           and get the min/max
        """
        for v in cd._current_variables:
            # we need a udunits time string in order for this to work
            var = cd.nc.variables[v]
            if hasattr(var, 'units'):
                # assume this is time if 'since' is in the units string
                # or this is the 'T' axis
                if ('since' in var.units.lower() or
                    (hasattr(var, 'axis') and var.axis == 'T') or
                    (hasattr(var, 'standard_name') and
                     var.standard_name == 'time')):
                    try:
                        return DapHarvest.get_time_from_dim(var)
                    except:
                        return None, None
        return None, None




    def harvest(self):
        """
        Identify the type of CF dataset this is:
          * UGRID
          * CGRID
          * RGRID
          * DSG
        """

        try:
            cd = CommonDataset.open(self.service.get('url'))
        except Exception as e:
            app.logger.error("Could not open DAP dataset from '%s'\n"
                             "Exception %s: %s" % (self.service.get('url'),
                                                   type(e).__name__, e))
            return 'Not harvested'

        # rely on times in the file first over global atts for calculating
        # start/end times of dataset.
        tmin, tmax = self.get_min_max_time(cd)
        # if nothing was returned, try to get from global atts
        if (tmin == None and tmax == None and
            'time_coverage_start' in cd.metadata and
            'time_coverage_end' in cd.metadata):
            try:
                tmin, tmax = (parse(cd.metadata[t]) for t in
                                   ('time_coverage_start', 'time_coverage_end'))
            except ValueError:
                tmin, tmax = None, None
        # For DAP, the unique ID is the URL
        unique_id = self.service.get('url')

        with app.app_context():
            dataset = db.Dataset.find_one( { 'uid' : unicode(unique_id) } )
            if dataset is None:
                dataset = db.Dataset()
                dataset.uid = unicode(unique_id)
                dataset['active'] = True

        # Find service reference in Dataset.services and remove (to replace it)
        tmp = dataset.services[:]
        for d in tmp:
            if d['service_id'] == self.service.get('_id'):
                dataset.services.remove(d)

        # Parsing messages
        messages = []

        # NAME
        name = None
        try:
            name = unicode_or_none(cd.nc.getncattr('title'))
        except AttributeError:
            messages.append(u"Could not get dataset name.  No global attribute named 'title'.")

        # DESCRIPTION
        description = None
        try:
            description = unicode_or_none(cd.nc.getncattr('summary'))
        except AttributeError:
            messages.append(u"Could not get dataset description.  No global attribute named 'summary'.")

        # KEYWORDS
        keywords = []
        try:
            keywords = sorted(map(lambda x: unicode(x.strip()), cd.nc.getncattr('keywords').split(",")))
        except AttributeError:
            messages.append(u"Could not get dataset keywords.  No global attribute named 'keywords' or was not comma seperated list.")

        # VARIABLES
        prefix    = ""
        # Add additonal prefix mappings as they become available.
        try:
            standard_name_vocabulary = unicode(cd.nc.getncattr("standard_name_vocabulary"))

            cf_regex = [re.compile("CF-"), re.compile('http://www.cgd.ucar.edu/cms/eaton/cf-metadata/standard_name.html')]

            for reg in cf_regex:
                if reg.match(standard_name_vocabulary) is not None:
                    prefix = "http://mmisw.org/ont/cf/parameter/"
                    break
        except AttributeError:
            pass

        # Get variables with a standard_name
        std_variables = [cd.get_varname_from_stdname(x)[0] for x in self.get_standard_variables(cd.nc) if x not in self.STD_AXIS_NAMES and len(cd.nc.variables[cd.get_varname_from_stdname(x)[0]].shape) > 0]

        # Get variables that are not axis variables or metadata variables and are not already in the 'std_variables' variable
        non_std_variables = list(set([x for x in cd.nc.variables if x not in itertools.chain(_possibley, _possiblex, _possiblez, _possiblet, self.METADATA_VAR_NAMES, self.COMMON_AXIS_NAMES) and len(cd.nc.variables[x].shape) > 0 and x not in std_variables]))

        axis_names = DapHarvest.get_axis_variables(cd.nc)
        """
        var_to_get_geo_from = None
        if len(std_names) > 0:
            var_to_get_geo_from = cd.get_varname_from_stdname(std_names[-1])[0]
            messages.append(u"Variable '%s' with standard name '%s' was used to calculate geometry." % (var_to_get_geo_from, std_names[-1]))
        else:
            # No idea which variable to generate geometry from... try to factor variables with a shape > 1.
            try:
                var_to_get_geo_from = [x for x in variables if len(cd.nc.variables[x].shape) > 1][-1]
            except IndexError:
                messages.append(u"Could not find any non-axis variables to compute geometry from.")
            else:
                messages.append(u"No 'standard_name' attributes were found on non-axis variables.  Variable '%s' was used to calculate geometry." % var_to_get_geo_from)
        """

        # LOCATION (from Paegan)
        # Try POLYGON and fall back to BBOX

        # paegan does not support ugrid, so try to detect this condition and skip
        is_ugrid = False
        is_trajectory = False
        for vname, v in cd.nc.variables.iteritems():
            if 'cf_role' in v.ncattrs():
                if v.getncattr('cf_role') == 'mesh_topology':
                    is_ugrid = True
                    break
                elif v.getncattr('cf_role') == 'trajectory_id':
                    is_trajectory = True
                    break

        gj = None

        if is_ugrid:
            messages.append(u"The underlying 'Paegan' data access library does not support UGRID and cannot parse geometry.")
        elif is_trajectory:
            coord_names = {}
            # try to get info for x, y, z, t axes
            for v in itertools.chain(std_variables, non_std_variables):
                try:
                    coord_names = cd.get_coord_names(v, **axis_names)

                    if coord_names['xname'] is not None and \
                       coord_names['yname'] is not None:
                        break
                except (AssertionError, AttributeError, ValueError, KeyError):
                    pass
            else:
                messages.append(u"Trajectory discovered but could not detect coordinate variables using the underlying 'Paegan' data access library.")

            if 'xname' in coord_names:
                try:
                    xvar = cd.nc.variables[coord_names['xname']]
                    yvar = cd.nc.variables[coord_names['yname']]

                    # one less order of magnitude eg 390000 -> 10000
                    slice_factor = 10 ** (int(math.log10(xvar.size)) - 1)

                    # TODO: don't split x/y as separate arrays.  Refactor to
                    # use single numpy array instead with both lon/lat

                    # tabledap datasets must be treated differently than
                    # standard DAP endpoints.  Retrieve geojson instead of
                    # trying to access as a DAP endpoint
                    if 'erddap/tabledap' in unique_id:
                        # take off 's.' from erddap
                        gj = self.erddap_geojson_url(coord_names)
                        # type defaults to MultiPoint, change to LineString
                        coords = np.array(gj['coordinates'][::slice_factor] +
                                          gj['coordinates'][-1:])
                        xs = coords[:, 0]
                        ys = coords[:, 1]
                    else:
                        xs = np.concatenate((xvar[::slice_factor], xvar[-1:]))
                        ys = np.concatenate((yvar[::slice_factor], yvar[-1:]))
                    # both coords must be valid to have a valid vertex
                    # get rid of any nans and unreasonable lon/lats
                    valid_idx = ((~np.isnan(xs)) & (np.absolute(xs) <= 180) &
                                 (~np.isnan(ys)) & (np.absolute(ys) <= 90))

                    xs = xs[valid_idx]
                    ys = ys[valid_idx]
                    # Shapely seems to require float64 values or incorrect
                    # values will propagate for the generated lineString
                    # if the array is not numpy's float64 dtype
                    lineCoords = np.array([xs, ys]).T.astype('float64')

                    gj = mapping(asLineString(lineCoords))

                    messages.append(u"Variable %s was used to calculate "
                                    u"trajectory geometry, and is a "
                                    u"naive sampling." % v)

                except (AssertionError, AttributeError,
                        ValueError, KeyError, IndexError) as e:
                    app.logger.warn("Trajectory error occured: %s", e)
                    messages.append(u"Trajectory discovered but could not create a geometry.")

        else:
            for v in itertools.chain(std_variables, non_std_variables):
                try:
                    gj = mapping(cd.getboundingpolygon(var=v, **axis_names
                                                       ).simplify(0.5))
                except (AttributeError, AssertionError, ValueError,
                        KeyError, IndexError):
                    try:
                        # Returns a tuple of four coordinates, but box takes in four seperate positional argouments
                        # Asterik magic to expland the tuple into positional arguments
                        app.logger.exception("Error calculating bounding box")

                        # handles "points" aka single position NCELLs
                        bbox = cd.getbbox(var=v, **axis_names)
                        gj = self.get_bbox_or_point(bbox)

                    except (AttributeError, AssertionError, ValueError,
                            KeyError, IndexError):
                        pass

                if gj is not None:
                    # We computed something, break out of loop.
                    messages.append(u"Variable %s was used to calculate geometry." % v)
                    break

            if gj is None: # Try the globals
                gj = self.global_bounding_box(cd.nc)
                messages.append(u"Bounding Box calculated using global attributes")
            if gj is None:
                messages.append(u"The underlying 'Paegan' data access library could not determine a bounding BOX for this dataset.")
                messages.append(u"The underlying 'Paegan' data access library could not determine a bounding POLYGON for this dataset.")
                messages.append(u"Failed to calculate geometry using all of the following variables: %s" % ", ".join(itertools.chain(std_variables, non_std_variables)))





        # TODO: compute bounding box using global attributes


        final_var_names = []
        if prefix == "":
            messages.append(u"Could not find a standard name vocabulary.  No global attribute named 'standard_name_vocabulary'.  Variable list may be incorrect or contain non-measured quantities.")
            final_var_names = non_std_variables + std_variables
        else:
            final_var_names = non_std_variables + list(map(unicode, ["%s%s" % (prefix, cd.nc.variables[x].getncattr("standard_name")) for x in std_variables]))

        service = {
            'name':           name,
            'description':    description,
            'service_type':   self.service.get('service_type'),
            'service_id':     ObjectId(self.service.get('_id')),
            'data_provider':  self.service.get('data_provider'),
            'metadata_type':  u'ncml',
            'metadata_value': unicode(dataset2ncml(cd.nc, url=self.service.get('url'))),
            'time_min': tmin,
            'time_max': tmax,
            'messages':       map(unicode, messages),
            'keywords':       keywords,
            'variables':      map(unicode, final_var_names),
            'asset_type':     get_common_name(DapHarvest.get_asset_type(cd)),
            'geojson':        gj,
            'updated':        datetime.utcnow()
        }

        with app.app_context():
            dataset.services.append(service)
            dataset.updated = datetime.utcnow()
            dataset.save()

        ncdataset = Dataset(self.service.get('url'))
        scores = self.ccheck_dataset(ncdataset)
        metamap = self.metamap_dataset(ncdataset)

        try:
            metadata_rec = self.save_ccheck_dataset('ioos', dataset._id, scores, metamap)
        except Exception as e:
            metadata_rec = None
            app.logger.error("could not save compliancecheck/metamap information", exc_info=True)

        return "Harvested"

    def ccheck_dataset(self, ncdataset):
        with app.app_context():
            scores = None
            try:
                cs = ComplianceCheckerCheckSuite()
                groups = cs.run(ncdataset, 'ioos')
                scores = groups['ioos']
            except Exception as e:
                app.logger.warn("Caught exception doing Compliance Checker on Dataset: %s", e)

            return scores

    def metamap_dataset(self, ncdataset):
        with app.app_context():

            # gets a metamap document of this service using wicken
            beliefs = IOOSNCCheck.beliefs()
            ncnamespaces = {'nc':pb_namespaces['ncml']}

            doc = NetCDFDogma('nc', beliefs, ncdataset, namespaces=ncnamespaces)

            # now make a map out of this
            # @TODO wicken should make this easier

            m_names, m_units = ['Variable Names*','Variable Units*']
            metamap = {}
            for k in beliefs:
                try:
                    metamap[k] = getattr(doc, doc._fixup_belief(k)[0])
                except Exception as e:
                    app.logger.exception("Problem setting belief (%s)", k)

            metamap[m_names] = [] # Override the Wicken return to preserve the order
            metamap[m_units] = [] # Override the Wicken return to preserve the order


            # Wicken doesn't preserve the order between the names and the units,
            # so what you wind up with is two lists that can't be related, but we
            # want to keep the relationship between the name and the units

            for k in ncdataset.variables.iterkeys():
                var_name = k
                standard_name = getattr(ncdataset.variables[k], 'standard_name', '')
                units = getattr(ncdataset.variables[k], 'units', '')

                # Only map metadata where we have all three
                if var_name and standard_name and units:
                    metamap[m_names].append('%s (%s)' % (var_name, standard_name))
                    metamap[m_units].append(units)

            return metamap

    def save_ccheck_dataset(self, checker_name, dataset_id, scores, metamap):
        """
        Saves the result of ccheck_station and metamap
        """
        return self.save_ccheck_and_metadata(self.service._id,
                                             checker_name,
                                             dataset_id,
                                             u'dataset',
                                             scores,
                                             metamap)

    @staticmethod
    def get_bbox_or_point(bbox):
        """
        Determine whether the bounds are a single point or bounding box area
        """
        # first check if coordinates are within valid bounds
        if (all(abs(x) <= 180  for x in bbox[::2]) and
            all(abs(y) <= 90 for y in bbox[1::2])):
            if len(bbox) == 4 and bbox[0:2] == bbox[2:4]:
                return mapping(Point(bbox[0:2]))
            else:
                # d3 expects poly coordinates in clockwise order (?)
                return mapping(box(*bbox, ccw=False))
        else:
            # If the point/bbox lies outside of valid bounds, don't generate
            # geojson
            return None


    def global_bounding_box(self, ncdataset):
        ncattrs = ncdataset.ncattrs()
        attrs_list = [
            'geospatial_lat_min',
            'geospatial_lat_max',
            'geospatial_lat_units',
            'geospatial_lon_min',
            'geospatial_lon_max',
            'geospatial_lon_units'
        ]

        # Check that each of them is in the ncdatasets global
        for attr_name in attrs_list:
            if attr_name not in ncattrs:
                break
        else: # All of them were found
            # Sometimes the attributes are strings, which will cause the
            # box calculation to fail.  Just to be sure, cast to float
            try:
                lat_min = float(ncdataset.geospatial_lat_min)
                lat_max = float(ncdataset.geospatial_lat_max)
                lon_min = float(ncdataset.geospatial_lon_min)
                lon_max = float(ncdataset.geospatial_lon_max)
            except ValueError:
                app.logger.warning('Bbox calculation from global attributes '
                                   'failed.  Likely due to uncastable string '
                                   'to float value')
                return None

            geometry = self.get_bbox_or_point([lon_min, lat_min,
                                               lon_max, lat_max])
            return geometry
        return None
