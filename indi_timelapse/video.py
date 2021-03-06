import os
import io
import time
from pathlib import Path
import subprocess
import fcntl
import errno

from multiprocessing import Process
#from threading import Thread
import multiprocessing

logger = multiprocessing.get_logger()


VIDEO_LOCKFILE = '/tmp/timelapse_video.lock'


class VideoProcessWorker(Process):
    def __init__(self, idx, config, video_q):
        super(VideoProcessWorker, self).__init__()

        #self.threadID = idx
        self.name = 'VideoProcessWorker{0:03d}'.format(idx)

        self.config = config
        self.video_q = video_q

        self.f_lock = None


    def run(self):
        while True:
            v_dict = self.video_q.get()

            if v_dict.get('stop'):
                return

            try:
                self._getLock()  # get lock to prevent multiple videos from being concurrently generated
            except BlockingIOError as e:
                if e.errno == errno.EAGAIN:
                    logger.error('Failed to get exclusive lock: %s', str(e))
                    return

            timespec = v_dict['timespec']
            img_folder = v_dict['img_folder']


            if not img_folder.exists():
                logger.error('Image folder does not exist: %s', img_folder)
                return


            video_file = img_folder.joinpath('allsky-{0:s}.mp4'.format(timespec))

            if video_file.exists():
                logger.warning('Video is already generated: %s', video_file)
                return


            seqfolder = img_folder.joinpath('.sequence')

            if not seqfolder.exists():
                logger.info('Creating sequence folder %s', seqfolder)
                seqfolder.mkdir()


            # delete all existing symlinks in seqfolder
            rmlinks = list(filter(lambda p: p.is_symlink(), seqfolder.iterdir()))
            if rmlinks:
                logger.warning('Removing existing symlinks in %s', seqfolder)
                for l_p in rmlinks:
                    l_p.unlink()


            # find all files
            timelapse_files = list()
            self.getFolderFilesByExt(img_folder, timelapse_files)

            # Exclude empty files
            timelapse_files_nonzero= filter(lambda p: p.stat().st_size != 0, timelapse_files)

            logger.info('Creating symlinked files for timelapse')
            timelapse_files_sorted = sorted(timelapse_files_nonzero, key=lambda p: p.stat().st_mtime)
            for i, f in enumerate(timelapse_files_sorted):
                symlink_p = seqfolder.joinpath('{0:04d}.{1:s}'.format(i, self.config['IMAGE_FILE_TYPE']))
                symlink_p.symlink_to(f)


            start = time.time()

            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'image2',
                '-r', '{0:d}'.format(self.config['FFMPEG_FRAMERATE']),
                '-i', '{0:s}/%04d.{1:s}'.format(str(seqfolder), self.config['IMAGE_FILE_TYPE']),
                '-vcodec', 'libx264',
                '-b:v', '{0:s}'.format(self.config['FFMPEG_BITRATE']),
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '{0:s}'.format(str(video_file)),
            ]

            ffmpeg_subproc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=lambda: os.nice(19),
            )

            elapsed_s = time.time() - start
            logger.info('Timelapse generated in %0.4f s', elapsed_s)

            logger.info('FFMPEG output: %s', ffmpeg_subproc.stdout)

            # delete all existing symlinks in seqfolder
            rmlinks = list(filter(lambda p: p.is_symlink(), Path(seqfolder).iterdir()))
            if rmlinks:
                logger.warning('Removing existing symlinks in %s', seqfolder)
                for l_p in rmlinks:
                    l_p.unlink()


            # remove sequence folder
            try:
                seqfolder.rmdir()
            except OSError as e:
                logger.error('Cannote remove sequence folder: %s', str(e))


            self._releaseLock()


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


    def _getLock(self):
        logger.info('Get exclusive lock to generate video')
        lock_p = Path(VIDEO_LOCKFILE)

        if not lock_p.is_file():
            f_lock = io.open(str(lock_p), 'w+')
            f_lock.close()
            lock_p.chmod(0o644)

        self.f_lock = io.open(str(lock_p), 'w+')
        fcntl.flock(self.f_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Exclusive, non-blocking lock


    def _releaseLock(self):
        logger.info('Release exclusive lock')
        fcntl.flock(self.f_lock, fcntl.LOCK_UN)
        self.f_lock.close()

