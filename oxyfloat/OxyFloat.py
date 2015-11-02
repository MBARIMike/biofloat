import os
import re
import logging
import urllib2
import requests
import pandas as pd
import pydap.client
import pydap.exceptions
import xray

from bs4 import BeautifulSoup
from contextlib import closing
from requests.exceptions import ConnectionError

# Support Python 2.7 and 3.x
try:
    from io import StringIO
except ImportError:
    from cStringIO import StringIO

from exceptions import RequiredVariableNotPresent

class OxyFloat(object):
    '''Collection of methods for working with Argo profiling float data.
    '''

    # Jupyter Notebook defines a root logger, use that if it exists
    if logging.getLogger().handlers:
        _notebook_handler = logging.getLogger().handlers[0]
        logger = logging.getLogger()
    else:
        logger = logging.getLogger(__name__)
        _handler = logging.StreamHandler()
        _formatter = logging.Formatter('%(levelname)s %(asctime)s %(filename)s '
                                      '%(funcName)s():%(lineno)d %(message)s')
        _handler.setFormatter(_formatter)
        logger.addHandler(_handler)

    _log_levels = (logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG)

    # Literals for groups stored in local HDF file cache
    _STATUS = 'status'
    _GLOBAL_META = 'global_meta'
    _coordinates = {'PRES_ADJUSTED', 'LATITUDE', 'LONGITUDE', 'JULD'}
    _MAX_PROFILES = 10000000000
    cache_file_fmt = 'oxyfloat_age_{}_max_profiles_{:d}.hdf'

    def __init__(self, verbosity=0, cache_file=None, oxygen_required=True,
            status_url='http://argo.jcommops.org/FTPRoot/Argo/Status/argo_all.txt',
            global_url='ftp://ftp.ifremer.fr/ifremer/argo/ar_index_global_meta.txt',
            thredds_url='http://tds0.ifremer.fr/thredds/catalog/CORIOLIS-ARGO-GDAC-OBS',
            variables=('TEMP_ADJUSTED', 'PSAL_ADJUSTED', 'DOXY_ADJUSTED', 
                       'PRES_ADJUSTED', 'LATITUDE', 'LONGITUDE', 'JULD')):

        '''Initialize OxyFloat object.
        
        Args:
            verbosity (int): range(4), default=0
            cache_file (str): Defaults to oxyfloat_cache.hdf next to module
            oxygen_required (boolean): Save profile only if oxygen data exist
            status_url (str): Source URL for Argo status data, defaults to
                http://argo.jcommops.org/FTPRoot/Argo/Status/argo_all.txt
            global_url (str): Source URL for DAC locations, defaults to
                ftp://ftp.ifremer.fr/ifremer/argo/ar_index_global_meta.txt
            thredds_url (str): Base URL for THREDDS Data Server, defaults to
                http://tds0.ifremer.fr/thredds/catalog/CORIOLIS-ARGO-GDAC-OBS
            variables (list): Variables to extract from NetCDF files
        '''
        self.status_url = status_url
        self.global_url = global_url
        self.thredds_url = thredds_url
        self.variables = set(variables)

        self.logger.setLevel(self._log_levels[verbosity])
        self._oxygen_required = oxygen_required

        if cache_file:
            self.cache_file_requested = cache_file
            self.cache_file = cache_file
        else:
            # Write to same directory where this module is installed
            self.cache_file = os.path.abspath(os.path.join(
                              os.path.dirname(__file__), 'oxyfloat_cache.hdf'))

    def _put_df(self, df, name):
        '''Save Pandas DataFrame to local HDF file.
        '''
        store = pd.HDFStore(self.cache_file)
        self.logger.info('Saving DataFrame to name "%s" in file %s',
                                            name, self.cache_file)
        store[name] = df
        self.logger.debug('store.close()')
        store.close()

    def _get_df(self, name):
        '''Get Pandas DataFrame from local HDF file or raise KeyError.
        '''
        store = pd.HDFStore(self.cache_file)
        try:
            self.logger.debug('Getting "%s" from %s', name, self.cache_file)
            df = store[name]
        except KeyError:
            raise
        finally:
            self.logger.debug('store.close()')
            store.close()

        return df

    def _status_to_df(self):
        '''Read the data at status_url link and return it as a Pandas DataFrame.
        '''
        self.logger.info('Reading data from %s', self.status_url)
        req = requests.get(self.status_url)
        req.encoding = 'UTF-16LE'

        # Had to tell requests the encoding, StringIO makes the text 
        # look like a file object. Skip over leading BOM bytes.
        df = pd.read_csv(StringIO(req.text[1:]))
        return df

    def _global_meta_to_df(self):
        '''Read the data at global_url link and return it as a Pandas DataFrame.
        '''
        self.logger.info('Reading data from %s', self.global_url)
        with closing(urllib2.urlopen(self.global_url)) as r:
            df = pd.read_csv(r, comment='#')

        return df

    def _profile_to_dataframe(self, wmo, url):
        '''Return a Pandas DataFrame of profiling float data from data at url.
        '''
        self.logger.debug('Opening %s', url)
        ds = xray.open_dataset(url)

        self.logger.debug('Checking %s for our desired variables', url)
        for v in self.variables:
            if v not in ds.keys():
                raise RequiredVariableNotPresent('%s not in %s', v, url)

        # Make a DataFrame with a hierarchical index for better efficiency
        # Argo data have a N_PROF dimension always of length 1, hence the [0]
        tuples = [(wmo, ds['JULD'].values[0], ds['LONGITUDE'].values[0], 
                   ds['LATITUDE'].values[0], round(pres, 1))
                            for pres in ds['PRES_ADJUSTED'].values[0]]
        indices = pd.MultiIndex.from_tuples(tuples, names=['wmo', 'time', 
                                                    'lon', 'lat', 'pressure'])
        df = pd.DataFrame()
        # Add only non-coordinate variables to the DataFrame
        for v in self.variables ^ self._coordinates:
            try:
                s = pd.Series(ds[v].values[0], index=indices)
                self.logger.debug('Added %s to DataFrame', v)
                df[v] = s
            except KeyError:
                self.logger.warn('%s not in %s', v, url)
            except pydap.exceptions.ServerError as e:
                self.logger.error(e)

        return df

    def _url_to_naturalname(self, url):
        '''Remove HDFStore illegal characters from url and return key string.
        '''
        regex = re.compile(r"[^a-zA-Z0-9_]")
        return regex.sub('', url)

    def set_verbosity(self, verbosity):
        '''Change loglevel. 0: ERROR, 1: WARN, 2: INFO, 3:DEBUG.
        '''
        self.logger.setLevel(self._log_levels[verbosity])

    def get_oxy_floats_from_status(self, age_gte=340):
        '''Return a Pandas Series of floats that are identified to have oxygen,
        are not greylisted, and have an age greater or equal to age_gte. 

        Args:
            age_gte (int): Restrict to floats with data >= age, defaults to 340
        '''
        try:
            df = self._get_df(self._STATUS)
        except KeyError:
            self.logger.debug('Could not read status from cache, loading it.')
            self._put_df(self._status_to_df(), self._STATUS)
            df = self._get_df(self._STATUS)

        odf = df.query('(OXYGEN == 1) & (GREYLIST == 0) & (AGE != 0) & '
                       '(AGE >= {:d})'.format(age_gte))

        return odf['WMO'].tolist()

        #odf = df.loc[(df.loc[:, 'OXYGEN'] == 1) & 
        #             (df.loc[:, 'GREYLIST'] == 0) & 
        #             (df.loc[:, 'AGE'] > age_gte), :]

        #return odf.ix[:, 'WMO'].tolist()

    def get_dac_urls(self, desired_float_numbers):
        '''Return dictionary of Data Assembly Centers keyed by wmo number.

        Args:
            desired_float_numbers (list[str]): List of strings of float numbers
        '''
        try:
            df = self._get_df(self._GLOBAL_META)
        except KeyError:
            self.logger.debug('Could not read global_meta, putting it into cache.')
            self._put_df(self._global_meta_to_df(), self._GLOBAL_META)
            df = self._get_df(self._GLOBAL_META)

        dac_urls = {}
        for _, row in df.loc[:,['file']].iterrows():
            floatNum = row['file'].split('/')[1]
            if floatNum in desired_float_numbers:
                url = self.thredds_url
                url += '/'.join(row['file'].split('/')[:2])
                url += "/profiles/catalog.xml"
                dac_urls[floatNum] = url

        self.logger.debug('Found %s dac_urls', len(dac_urls))

        return dac_urls

    def get_profile_opendap_urls(self, catalog_url):
        '''Returns an iterable to the opendap urls for the profiles in catalog.
        The `catalog_url` is the .xml link for a directory on a THREDDS Data 
        Server.
        '''
        urls = []
        try:
            self.logger.debug("Parsing %s", catalog_url)
            req = requests.get(catalog_url)
        except ConnectionError as e:
            self.logger.error('Cannot open catalog_url = %s', catalog_url)
            self.logger.exception(e)
            return urls

        soup = BeautifulSoup(req.text, 'html.parser')

        # Expect that this is a standard TDS with dodsC used for OpenDAP
        base_url = '/'.join(catalog_url.split('/')[:4]) + '/dodsC/'

        # Pull out <dataset ... urlPath='...nc'> attributes from the XML
        for e in soup.findAll('dataset', attrs={'urlpath': re.compile("nc$")}):
            urls.append(base_url + e['urlpath'])

        return urls

    def _get_cache_file_parms(self, max_profiles):
        '''Adjust max_profiles setting based on cache_file being used
        so as not to cause downloading of additional unwanted data.
        Returns potentially adjusted max_profiles.
        '''
        adjusted_max_profiles = max_profiles
        try:
            p = re.compile('max_profiles_([0-9]+)')
            m = p.search(self.cache_file_requested)
            cache_file_max = int(m.group(1))
            if not max_profiles or max_profiles > cache_file_max:
                self.logger.warn("Requested max_profiles %s exceeds requested "
                        "cache file's: %s", max_profiles, cache_file_max)
                self.logger.info("Setting max_profiles to %s", cache_file_max)
                adjusted_max_profiles = cache_file_max
        except AttributeError:
            pass

        return adjusted_max_profiles

    def _validate_oxygen(self, df, url):
        '''Return empty DataFrame if no valid oxygen otherwise return df.
        '''
        if df['DOXY_ADJUSTED'].dropna().empty:
            self.logger.warn('Oxygen is all NaNs in %s', url)
            df = pd.DataFrame()

        return df

    def _save_profile(self, url, count, opendap_urls, wmo, key):
        '''Put profile data into the local HDF cache.
        '''
        try:
            self.logger.info('Profile %s of %s from %s', count, 
                              len(opendap_urls), url)
            df = self._profile_to_dataframe(wmo, url)
            if self._oxygen_required:
                df = self._validate_oxygen(df, url)
            self.logger.debug(df.head())
        except RequiredVariableNotPresent as e:
            self.logger.warn(str(e))
            df = pd.DataFrame()

        self._put_df(df, key)

        return df

    def get_float_dataframe(self, wmo_list, max_profiles=None):
        '''Returns Pandas DataFrame for all the profile data from wmo_list.
        Uses cached data if present, populates cache if not present.  If 
        max_profiles is set to a number then data from only those profiles
        will be returned, this is useful for testing or for getting just 
        the most recent data from the float.
        '''
        max_profiles = self._get_cache_file_parms(max_profiles)
        if not max_profiles:
            max_profiles = self._MAX_PROFILES

        float_df = pd.DataFrame()
        for f, (wmo, dac_url) in enumerate(self.get_dac_urls(wmo_list).iteritems()):
            self.logger.info('Float %s of %s, wmo = %s', f + 1, len(wmo_list), wmo)
            opendap_urls = self.get_profile_opendap_urls(dac_url)
            for i, url in enumerate(opendap_urls):
                if i > max_profiles:
                    self.logger.info('Stopping at max_profiles = %s', max_profiles)
                    break
                key = self._url_to_naturalname(url)
                try:
                    df = self._get_df(key)
                except KeyError:
                    df = self._save_profile(url, i, opendap_urls, wmo, key)

                float_df = float_df.append(df)

        return float_df

