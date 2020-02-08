import signal
import time
import json
import picamera
import queue
import multiprocessing as mp

import logging
logger = logging.getLogger()
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_format = logging.Formatter("%(asctime)s - %(name)s - %(threadName)s - %(levelname)s - %(message)s")
stream_handler.setFormatter(stream_format)
logger.addHandler(stream_handler)
logger.setLevel(logging.DEBUG)

disable_loggers = ["urllib3.connectionpool"]
for name, logger in logging.root.manager.loggerDict.items():
    if name in disable_loggers:
        logger.disabled = True

from workers import BroadcastReassembled, InferenceWorker, Flusher, session
from requests_toolbelt.adapters.source import SourceAddressAdapter

class GracefullKiller():
    kill_now = False
    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
    
    def exit_gracefully(self, signum, frame):
        self.kill_now = True

class WorkerPool(mp.Process):
    def __init__(self, name, worker, pool_size, *args, **kwargs):
        super(WorkerPool, self).__init__(name=name)
        self.event_stopper = mp.Event()
        self.Worker = worker
        self.pool_size = pool_size
        self.args = args
        self.kwargs = kwargs

    def run(self):
        logger.info("spawning workers on separate process")
        pool = [self.Worker(self.event_stopper, *self.args, **self.kwargs, name="{}-Worker-{}".format(self.name, i)) for i in range(self.pool_size)]
        [worker.start() for worker in pool]
        while not self.event_stopper.is_set():
            time.sleep(0.001)
        logger.info("stoppping workers on separate process")
        [worker.join() for worker in pool]

    def stop(self):
        self.event_stopper.set()

class DistributeFramesAndInfer():
    def __init__(self, pool_cfg, worker_cfg):
        self.frame_num = 0
        self.in_queue = mp.Queue()
        self.out_queue = mp.Queue()
        for key, value in pool_cfg.items():
            setattr(self, key, value)
        self.pool = WorkerPool("InferencePool", InferenceWorker, self.workers, self.in_queue, self.out_queue, worker_cfg)
        self.pool.start()

    def write(self, buf):
        if buf.startswith(b"\xff\xd8"):
            # start of new frame; close the old one (if any) and
            if self.frame_num % self.pick_every_nth_frame == 0:
                self.in_queue.put({
                    "frame_num": self.frame_num,
                    "jpeg": buf
                })
            self.frame_num += 1

    def stop(self):
        self.pool.stop()
        self.pool.join()
        qs = [self.in_queue, self.out_queue]
        [q.cancel_join_thread() for q in qs]

    def get_queues(self):
        return self.in_queue, self.out_queue

def main():
    killer = GracefullKiller()

    try:
        file = open('config.json')
        cfg = json.load(file)
        file.close()
    except Exception as error:
        logger.critical(str(error), exc_info = 1)
        return
    
    # interface_ip = "172.20.10.2"
    # session.mount('http://', SourceAddressAdapter(interface_ip))

    source_cfg = cfg["video_source"]
    broadcast_cfg = cfg["broadcaster"]
    pool_cfg = cfg["inferencing_pool"]
    worker_cfg = cfg["inferencing_worker"]
    gen_cfg = cfg["general"]
    
    # workers on a separate process to run inference on the data
    logger.info("initializing pool w/ " + str(pool_cfg["workers"]) + " workers")
    output = DistributeFramesAndInfer(pool_cfg, worker_cfg)
    frames_queue, inferenced_queue = output.get_queues()
    logger.info("initialized worker pool")

    # a single worker in a separate process to reassemble the data
    reassembler = BroadcastReassembled(inferenced_queue, broadcast_cfg, name="BroadcastReassembled")
    reassembler.start()

    # a single thread to flush the producing queue
    # when there are too many frames in the pipe
    flusher = Flusher(frames_queue, threshold=gen_cfg["frame_count_threshold"], name="Flusher")
    flusher.start()

    # start the pi camera
    with picamera.PiCamera() as camera:
        # configure the camera
        camera.sensor_mode = source_cfg["sensor_mode"]
        camera.resolution = source_cfg["resolution"]
        camera.framerate = source_cfg["framerate"]
        logger.info("picamera initialized w/ mode={} resolution={} framerate={}".format(
            camera.sensor_mode, camera.resolution, camera.framerate
        ))

        # start recording both to disk and to the queue
        camera.start_recording(output=gen_cfg["output_file"], format="h264", splitter_port=0, bitrate=10000000)
        camera.start_recording(output=output, format="mjpeg", splitter_port=1, bitrate=10000000, quality=95)
        logger.info("started recording to file and to queue")

        # wait until SIGINT is detected
        while not killer.kill_now:
            camera.wait_recording(timeout=0.5, splitter_port=0)
            camera.wait_recording(timeout=0.5, splitter_port=1)
            logger.info('frames qsize: {}, inferenced qsize: {}'.format(output.in_queue.qsize(), output.out_queue.qsize()))

        # stop recording
        logger.info("gracefully exiting")
        camera.stop_recording(splitter_port=0)
        camera.stop_recording(splitter_port=1)
        output.stop() 

    reassembler.stop()
    flusher.stop()

if __name__ == "__main__":
    main()