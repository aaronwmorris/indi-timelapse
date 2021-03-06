import sys
import time
import io
import json
from pathlib import Path
from datetime import datetime
from datetime import timedelta
import copy
import math
import signal

import ephem

from multiprocessing import Pipe
from multiprocessing import Queue
from multiprocessing import Value
import multiprocessing

import PyIndi

from .indi import IndiClient
from .image import ImageProcessWorker
from .video import VideoProcessWorker
from .uploader import FileUploader
from .exceptions import TimeOutException

logger = multiprocessing.get_logger()


class IndiTimelapse(object):

    def __init__(self, f_config_file):
        self.config = json.loads(f_config_file.read())
        f_config_file.close()

        self.config_file = f_config_file.name

        self.image_q = Queue()
        self.indiblob_status_receive, self.indiblob_status_send = Pipe(duplex=False)
        self.indiclient = None
        self.device = None
        self.exposure_v = Value('f', copy.copy(self.config['CCD_EXPOSURE_DEF']))
        self.gain_v = Value('i', copy.copy(self.config['INDI_CONFIG_NIGHT']['GAIN_VALUE']))
        self.bin_v = Value('i', copy.copy(self.config['INDI_CONFIG_NIGHT']['BIN_VALUE']))
        self.sensortemp_v = Value('f', 0)
        self.night_v = Value('i', 1)

        self.night_sun_radians = math.radians(float(self.config['NIGHT_SUN_ALT_DEG']))

        self.image_worker = None
        self.image_worker_idx = 0

        self.video_worker = None
        self.video_q = Queue()
        self.video_worker_idx = 0

        self.save_fits = False
        self.save_images = True

        self.upload_worker = None
        self.upload_q = Queue()
        self.upload_worker_idx = 0


        self.__state_to_str = { PyIndi.IPS_IDLE: 'IDLE', PyIndi.IPS_OK: 'OK', PyIndi.IPS_BUSY: 'BUSY', PyIndi.IPS_ALERT: 'ALERT' }
        self.__switch_types = { PyIndi.ISR_1OFMANY: 'ONE_OF_MANY', PyIndi.ISR_ATMOST1: 'AT_MOST_ONE', PyIndi.ISR_NOFMANY: 'ANY'}
        self.__type_to_str = { PyIndi.INDI_NUMBER: 'number', PyIndi.INDI_SWITCH: 'switch', PyIndi.INDI_TEXT: 'text', PyIndi.INDI_LIGHT: 'light', PyIndi.INDI_BLOB: 'blob', PyIndi.INDI_UNKNOWN: 'unknown' }

        self.base_dir = Path(__file__).parent.parent.absolute()

        self.generate_timelapse_flag = False   # This is updated once images have been generated

        signal.signal(signal.SIGALRM, self.alarm_handler)
        signal.signal(signal.SIGHUP, self.hup_handler)



    def hup_handler(self, signum, frame):
        logger.warning('Caught HUP signal, reconfiguring')

        with io.open(self.config_file, 'r') as f_config_file:
            try:
                c = json.loads(f_config_file.read())
                f_config_file.close()
            except json.JSONDecodeError as e:
                logger.error('Error decoding json: %s', str(e))
                f_config_file.close()
                return

        # overwrite config
        self.config = c
        self.night_sun_radians = math.radians(float(self.config['NIGHT_SUN_ALT_DEG']))

        nighttime = self.is_night()

        # reconfigure if needed
        if self.night_v.value != int(nighttime):
            self.dayNightReconfigure(nighttime)

        self._stopImageProcessWorker()
        self._stopVideoProcessWorker()
        self._stopImageUploadWorker()

        # Restart worker with new config
        self._startImageProcessWorker()
        self._startVideoProcessWorker()
        self._startImageUploadWorker()


    def alarm_handler(self, signum, frame):
        raise TimeOutException()


    def _initialize(self):
        self._startImageProcessWorker()
        self._startVideoProcessWorker()
        self._startImageUploadWorker()

        # instantiate the client
        self.indiclient = IndiClient(
            self.config,
            self.indiblob_status_send,
            self.image_q,
        )

        # set roi
        #indiclient.roi = (270, 200, 700, 700) # region of interest for my allsky cam

        # set indi server localhost and port 7624
        self.indiclient.setServer("localhost", 7624)

        # connect to indi server
        logger.info("Connecting to indiserver")
        if (not(self.indiclient.connectServer())):
            logger.error("No indiserver running on %s:%d - Try to run", self.indiclient.getHost(), self.indiclient.getPort())
            logger.error("  indiserver indi_simulator_telescope indi_simulator_ccd")
            sys.exit(1)

        # give devices a chance to register
        time.sleep(8)

        # connect to all devices
        for d in self.indiclient.getDevices():
            logger.info('Found device %s', d.getDeviceName())

            if d.getDeviceName() == self.config['CCD_NAME']:
                logger.info('Connecting to device %s', d.getDeviceName())
                self.indiclient.connectDevice(d.getDeviceName())
                self.device = d

        # set default device in indiclient
        self.indiclient.device = self.device

        # set BLOB mode to BLOB_ALSO
        logger.info('Set BLOB mode')
        self.indiclient.setBLOBMode(1, self.device.getDeviceName(), None)


        ### Perform device config
        self._configureCcd(
            self.config['INDI_CONFIG_NIGHT'],
        )



    def _startImageProcessWorker(self):
        if self.image_worker:
            if self.image_worker.is_alive():
                return

        self.image_worker_idx += 1

        logger.info('Starting ImageProcessorWorker process')
        self.image_worker = ImageProcessWorker(
            self.image_worker_idx,
            self.config,
            self.image_q,
            self.upload_q,
            self.exposure_v,
            self.gain_v,
            self.bin_v,
            self.sensortemp_v,
            self.night_v,
            save_fits=self.save_fits,
            save_images=self.save_images,
        )
        self.image_worker.start()


    def _stopImageProcessWorker(self):
        if self.image_worker:
            if not self.image_worker.is_alive():
                return

        logger.info('Stopping ImageProcessorWorker process')
        self.image_q.put({ 'stop' : True })
        self.image_worker.join()


    def _startVideoProcessWorker(self):
        if self.video_worker:
            if self.video_worker.is_alive():
                return

        self.video_worker_idx += 1

        logger.info('Starting VideoProcessorWorker process')
        self.video_worker = VideoProcessWorker(
            self.video_worker_idx,
            self.config,
            self.video_q,
        )
        self.video_worker.start()


    def _stopVideoProcessWorker(self):
        if self.video_worker:
            if not self.video_worker.is_alive():
                return

        logger.info('Stopping VideoProcessorWorker process')
        self.video_q.put({ 'stop' : True })
        self.video_worker.join()


    def _startImageUploadWorker(self):
        if self.upload_worker:
            if self.upload_worker.is_alive():
                return

        self.upload_worker_idx += 1

        logger.info('Starting FileUploader process %d', self.upload_worker_idx)
        self.upload_worker = FileUploader(
            self.upload_worker_idx,
            self.config,
            self.upload_q,
        )

        self.upload_worker.start()


    def _stopImageUploadWorker(self):
        if self.upload_worker:
            if not self.upload_worker.is_alive():
                return

        logger.info('Stopping ImageUploadWorker process')
        self.upload_q.put({ 'stop' : True })
        self.upload_worker.join()


    def _configureCcd(self, indi_config):
        ### Configure CCD Properties
        for k, v in indi_config['PROPERTIES'].items():
            logger.info('Setting property %s', k)
            self.indiclient.set_number(k, v)


        ### Configure CCD Switches
        for k, v in indi_config['SWITCHES'].items():
            logger.info('Setting switch %s', k)
            self.indiclient.set_switch(k, on_switches=v['on'], off_switches=v.get('off', []))

        ### Configure controls
        #self.indiclient.set_controls(indi_config.get('CONTROLS', {}))

        # Update shared gain value
        gain_value = indi_config.get('GAIN_VALUE')
        with self.gain_v.get_lock():
            self.gain_v.value = int(gain_value)

        bin_value = indi_config.get('BIN_VALUE')
        with self.bin_v.get_lock():
            self.bin_v.value = int(bin_value)

        logger.info('Gain set to %d', self.gain_v.value)
        logger.info('Binning set to %d', self.bin_v.value)

        # Sleep after configuration
        time.sleep(1.0)


    def run(self):

        self._initialize()

        ### main loop starts
        while True:
            # restart worker if it has failed
            if not self.image_worker.is_alive():
                self._startImageProcessWorker()

            if not self.video_worker.is_alive():
                self._startVideoProcessWorker()

            if not self.upload_worker.is_alive():
                self._startImageUploadWorker()


            nighttime = self.is_night()
            #logger.info('self.night_v.value: %r', self.night_v.value)
            #logger.info('is night: %r', nighttime)

            if not nighttime and not self.config['DAYTIME_CAPTURE']:
                logger.info('Daytime capture is disabled')
                time.sleep(60)
                continue

            temp = self.device.getNumber("CCD_TEMPERATURE")
            if temp:
                with self.sensortemp_v.get_lock():
                    logger.info("Sensor temperature: %0.1f", temp[0].value)
                    self.sensortemp_v.value = temp[0].value


            ### Change between day and night
            if self.night_v.value != int(nighttime):
                self.expireImages()  # cleanup old images and folders

                self.dayNightReconfigure(nighttime)

                if not nighttime and self.generate_timelapse_flag:
                    ### Generate timelapse at end of night
                    yesterday_ref = datetime.now() - timedelta(days=1)
                    timespec = yesterday_ref.strftime('%Y%m%d')
                    self._generateNightTimelapse(timespec)

                if nighttime and self.generate_timelapse_flag:
                    ### Generate timelapse at end of day
                    today_ref = datetime.now()
                    timespec = today_ref.strftime('%Y%m%d')
                    self._generateDayTimelapse(timespec)



            start = time.time()

            try:
                self.shoot(self.exposure_v.value)
            except TimeOutException as e:
                logger.error('Timeout: %s', str(e))
                time.sleep(5.0)
                continue

            shoot_elapsed_s = time.time() - start
            logger.info('shoot() completed in %0.4f s', shoot_elapsed_s)

            # should take far less than 5 seconds here
            signal.alarm(5)


            if nighttime:
                # always indicate timelapse generation at night
                self.generate_timelapse_flag = True  # indicate images have been generated for timelapse
            elif self.config['DAYTIME_TIMELAPSE']:
                # must be day time
                self.generate_timelapse_flag = True  # indicate images have been generated for timelapse

            try:
                self.indiblob_status_receive.recv()  # wait until image is received
            except TimeOutException:
                logger.error('Timeout waiting on exposure, continuing')
                time.sleep(5.0)
                continue

            signal.alarm(0)  # reset timeout


            full_elapsed_s = time.time() - start
            logger.info('Exposure received in %0.4f s', full_elapsed_s)

            # sleep for the remaining eposure period
            remaining_s = float(self.config['EXPOSURE_PERIOD']) - full_elapsed_s
            if remaining_s > 0:
                logger.info('Sleeping for additional %0.4f s', remaining_s)
                time.sleep(remaining_s)


    def dayNightReconfigure(self, nighttime):
        logger.warning('Change between night and day')
        with self.night_v.get_lock():
            self.night_v.value = int(nighttime)

        if nighttime:
            self._configureCcd(
                self.config['INDI_CONFIG_NIGHT'],
            )
        else:
            self._configureCcd(
                self.config['INDI_CONFIG_DAY'],
            )

        # Sleep after reconfiguration
        time.sleep(1.0)


    def is_night(self):
        obs = ephem.Observer()
        obs.lon = str(self.config['LOCATION_LONGITUDE'])
        obs.lat = str(self.config['LOCATION_LATITUDE'])
        obs.date = datetime.utcnow()  # ephem expects UTC dates

        sun = ephem.Sun()
        sun.compute(obs)

        logger.info('Sun altitude: %s', sun.alt)
        return sun.alt < self.night_sun_radians



    def darks(self):

        self.save_fits = True
        self.save_images = False

        self._initialize()

        ### NIGHT DARKS ###
        self._configureCcd(
            self.config['INDI_CONFIG_NIGHT'],
        )

        ### take darks
        dark_exposures = (self.config['CCD_EXPOSURE_MIN'], 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
        for exp in dark_exposures:
            filename_t = 'dark_{0:d}s_gain{1:d}_bin{2:d}.{3:s}'.format(int(exp), self.gain_v.value, self.bin_v.value, '{1}')

            start = time.time()

            self.indiclient.filename_t = filename_t
            self.shoot(float(exp))
            self.indiblob_status_receive.recv()  # wait until image is received

            elapsed_s = time.time() - start

            logger.info('Exposure received in %0.4f s', elapsed_s)

            logger.info('Sleeping for additional %0.4f s', 1.0)
            time.sleep(1.0)


        ### DAY DARKS ###
        self._configureCcd(
            self.config['INDI_CONFIG_DAY'],
        )


        ### take darks
        dark_exposures = (self.config['CCD_EXPOSURE_MIN'],)  # day will rarely exceed the minimum exposure
        for exp in dark_exposures:
            filename_t = 'dark_{0:d}s_gain{1:d}.{2:s}'.format(int(exp), self.gain_v.value, '{1}')

            start = time.time()

            self.indiclient.filename_t = filename_t
            self.shoot(float(exp))
            self.indiblob_status_receive.recv()  # wait until image is received

            elapsed_s = time.time() - start

            logger.info('Exposure received in %0.4f s', elapsed_s)

            logger.info('Sleeping for additional %0.4f s', 1.0)
            time.sleep(1.0)



        ### stop image processing worker
        self._stopImageProcessWorker()
        self._stopVideoProcessWorker()
        self._stopImageUploadWorker()


        ### INDI disconnect
        self.indiclient.disconnectServer()


    def generateAllTimelapse(self, timespec, day=True, night=True):
        if day:
            self._generateDayTimelapse(timespec)

        if night:
            self._generateNightTimelapse(timespec)


    def generateDayTimelapse(self, timespec):
        self._startVideoProcessWorker()
        self._generateDayTimelapse(timespec)
        self._stopVideoProcessWorker()


    def _generateDayTimelapse(self, timespec):
        if self.image_worker:
            self._stopImageProcessWorker()

        if self.upload_worker:
            self._stopImageUploadWorker()

        self._startVideoProcessWorker()

        img_base_folder = self.base_dir.joinpath('images', '{0:s}'.format(timespec))

        logger.warning('Generating day time timelapse for %s', timespec)
        img_day_folder = img_base_folder.joinpath('day')

        self.video_q.put({ 'timespec' : timespec, 'img_folder' : img_day_folder })


    def generateNightTimelapse(self, timespec):
        self._startVideoProcessWorker()
        self._generateNightTimelapse(timespec)
        self._stopVideoProcessWorker()


    def _generateNightTimelapse(self, timespec):
        if self.image_worker:
            self._stopImageProcessWorker()

        if self.upload_worker:
            self._stopImageUploadWorker()

        img_base_folder = self.base_dir.joinpath('images', '{0:s}'.format(timespec))

        logger.warning('Generating day time timelapse for %s', timespec)
        img_day_folder = img_base_folder.joinpath('night')

        self.video_q.put({ 'timespec' : timespec, 'img_folder' : img_day_folder })


    def shoot(self, exposure, sync=True, timeout=None):
        if not timeout:
            timeout = (exposure * 2.0) + 5.0
        logger.info('Taking %0.6f s exposure (gain %d)', exposure, self.gain_v.value)
        self.indiclient.set_number('CCD_EXPOSURE', {'CCD_EXPOSURE_VALUE': exposure}, sync=sync, timeout=timeout)



    def expireImages(self, days=None):
        ### This needs to be run before generating a timelapse

        if not days:
            days = self.config['IMAGE_EXPIRE_DAYS']

        img_root_folder = self.base_dir.joinpath('images')

        # Orphaned symlinks need to be removed
        symlink_list = list()
        self.getFolderSymlinks(img_root_folder, symlink_list)
        for f in symlink_list:
            logger.info('Removing orphaned symlink: %s', f)

            try:
                f.unlink()
            except OSError as e:
                logger.error('Cannot remove symlink: %s', str(e))

        # Old image files need to be pruned
        file_list = list()
        self.getFolderFilesByExt(img_root_folder, file_list, extension_list=['jpg', 'jpeg', 'png', 'tif', 'tiff'])

        cutoff_age = datetime.now() - timedelta(days=days)

        old_files = filter(lambda p: p.stat().st_mtime < cutoff_age.timestamp(), file_list)
        for f in old_files:
            logger.info('Removing old image: %s', f)

            try:
                f.unlink()
            except OSError as e:
                logger.error('Cannot remove file: %s', str(e))


        # Remove empty folders
        dir_list = list()
        self.getFolderFolders(img_root_folder, dir_list)

        empty_dirs = filter(lambda p: not any(p.iterdir()), dir_list)
        for d in empty_dirs:
            logger.info('Removing empty directory: %s', d)

            try:
                d.rmdir()
            except OSError as e:
                logger.error('Cannot remove folder: %s', str(e))


    def getFolderSymlinks(self, folder, symlink_list):
        for item in Path(folder).iterdir():
            if item.is_symlink():
                symlink_list.append(item)
            elif item.is_dir():
                self.getFolderSymlinks(item, symlink_list)  # recursion


    def getFolderFilesByExt(self, folder, file_list, extension_list=None):
        if not extension_list:
            extension_list = [self.config['IMAGE_FILE_TYPE']]

        logger.info('Searching for image files in %s', folder)

        dot_extension_list = ['.{0:s}'.format(e) for e in extension_list]

        for item in Path(folder).iterdir():
            if item.is_file() and item.suffix in dot_extension_list:
                file_list.append(item)
            elif item.is_dir():
                self.getFolderFilesByExt(item, file_list, extension_list=extension_list)  # recursion


    def getFolderFolders(self, folder, dir_list):
        for item in Path(folder).iterdir():
            if item.is_dir():
                dir_list.append(item)
                self.getFolderFolders(item, dir_list)  # recursion

