import configparser
import time
import sys
import numpy
import cv2
import imutils
import grequests
import json
import threading
import socket

from camerafeed.peopletracker import PeopleTracker
from camerafeed.tripline import Tripline


host='192.168.0.6'
port=4009
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
s.bind((host,port))
s.listen(True)
conn,addr = s.accept()
#class SensorGet(threading.Thread):
#       def __init__(self):
#                super(SensorGet,self).__init__()
#                self.server = "192.168.0.6"
#                self.port = 5007

#       def run(self):
#                global s_data
#                s2 = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
#                s2.bind((self.server,self.port))
#                s2.listen(1)
#                client,addr = s2.accept()
#                while(1):
#                        s_data=client.recv(1024)
#t=SensorGet()
#t.start()

def recvall(sock,count):
        buf = b''
        while count:
            newbuf = sock.recv(count)
            if not newbuf : return None
            buf += newbuf
            count -= len(newbuf)
        return buf

class CameraFeed:
    # frame dimension (calculated below in go)
    _frame_width = 0
    _frame_height = 0

    # how many frames processed
    _frame = 0

    def __init__(self, source=0, crop_x1=0, crop_y1=0, crop_x2=500, crop_y2=500, max_width=640, b_and_w=False,
                 hog_win_stride=6, hog_padding=8, hog_scale=1.05, mog_enabled=False, people_options=None, lines=None,
                 font=cv2.FONT_HERSHEY_SIMPLEX, endpoint=None, pi=False, show_window=True, to_stdout=False,
                 save_first_frame=False, quit_after_first_frame=False):

        self.__dict__.update(locals())

    def go_config(self, config_path=None):

        # load config
        config = configparser.ConfigParser()
        config.read(config_path)

        # remote host settings
        self.endpoint = config.get('host', 'endpoint', fallback=None)

        # platform
        self.pi = config.getboolean('platform', 'pi')
        self.to_stdout = config.getboolean('platform', 'to_stdout')
        self.show_window = config.getboolean('platform', 'show_window')
        self.save_first_frame = config.getboolean('platform', 'save_first_frame')
        self.quit_after_first_frame = config.getboolean('platform', 'quit_after_first_frame')

        # video source settings
        self.crop_x1 = config.getint('video_source', 'frame_x1')
        self.crop_y1 = config.getint('video_source', 'frame_y1')
        self.crop_x2 = config.getint('video_source', 'frame_x2')
        self.crop_y2 = config.getint('video_source', 'frame_y2')
        self.max_width = config.getint('video_source', 'max_width')
        self.b_and_w = config.getboolean('video_source', 'b_and_w')

        # hog settings
        self.hog_win_stride = config.getint('hog', 'win_stride')
        self.hog_padding = config.getint('hog', 'padding')
        self.hog_scale = config.getfloat('hog', 'scale')

        # mog settings
        self.mog_enabled = config.getboolean('mog', 'enabled')
        if self.mog_enabled:
            self.mogbg = cv2.createBackgroundSubtractorMOG2()

        # setup lines
        lines = []
        total_lines = config.getint('triplines', 'total_lines')

        for idx in range(total_lines):
            key = 'line%d' % (idx + 1)
            start = eval(config.get('triplines', '%s_start' % key))
            end = eval(config.get('triplines', '%s_end' % key))
            buffer = config.getint('triplines', '%s_buffer' % key, fallback=10)
            direction_1 = config.get('triplines', '%s_direction_1' % key, fallback='Up')
            direction_2 = config.get('triplines', '%s_direction_2' % key, fallback='Down')
            line = Tripline(point_1=start, point_2=end, buffer_size=buffer, direction_1=direction_1,
                            direction_2=direction_2)
            lines.append(line)

        self.lines = lines
        self.source = config.get('video_source', 'source')
        self.people_options = dict(config.items('person'))

        self.go()
    

    def go(self):
        global sensor_s
        # setup HUD
        self.last_time = time.time()

        # opencv 3.x bug??
        cv2.ocl.setUseOpenCL(False)

        # people tracking
        self.finder = PeopleTracker(people_options=self.people_options)

        # STARTS HERE
        # connect to camera
        if self.pi:
            time.sleep(1)  # let camera warm up

        else:
            self.camera = cv2.VideoCapture(0)

        # setup detectors
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

        # feed in video
        if self.pi:
            while(1):
                sensor = recvall(conn,8)
                sensor_s = sensor.decode("utf-8")
                b_length = recvall(conn,16)
                length = b_length.decode("utf-8")
                stringData = recvall(conn,int(length))
                data = numpy.fromstring(stringData,dtype='uint8')
                decimg=cv2.imdecode(data,1)
                self.process(decimg)

                if self.quit_after_first_frame or cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        else:

            while self.camera.isOpened():

                rval, frame = self.camera.read()
                self.process(frame)

                if self.quit_after_first_frame or cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    def process(self, frame):

        global sensor_s
        if self.b_and_w:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        frame = self.crop_and_resize(frame)

        print_frame_size = self._frame_height == 0

        self._frame_height = frame.shape[0]
        self._frame_width = frame.shape[1]

        if print_frame_size and not self.to_stdout:
            print('resized video to %dx%d' % (self._frame_width, self._frame_height))

        sensor_s=sensor_s[0:3] #받은 데이터뒤에 null있으므로 문자열만 잘라서 센서값 판별
        #print(sensor_s)
        
        if sensor_s == 'red': #센서값이 레드일때만 보행자 알고리즘 실행
                frame = self.apply_mog(frame)
                frame = self.handle_the_people(frame)
                frame = self.render_hud(frame)
        
        if self.show_window:
            cv2.imshow('Camerafeed', frame)

        if self.to_stdout:
            sys.stdout.write(frame.tostring())
            # string = "".join(map(chr, frame.tostring()))
            # sys.stdout.write(string)

        if self.save_first_frame and self._frame == 0:
            cv2.imwrite('first_frame.png', frame)

    # help us crop/resize frames as they come in
    def crop_and_resize(self, frame):

        frame = frame[self.crop_y1:self.crop_y2, self.crop_x1:self.crop_x2]
        frame = imutils.resize(frame, width=min(self.max_width, frame.shape[1]))

        return frame

    # apply background subtraction if needed
    def apply_mog(self, frame):
        if self.mog_enabled:
            mask = self.mogbg.apply(frame)
            frame = cv2.bitwise_and(frame, frame, mask=mask)

        return frame

    # all the data that overlays the video
    def render_hud(self, frame):
        this_time = time.time()
        diff = this_time - self.last_time
        fps = 1 / diff
        message = 'FPS: %d' % fps
        # print(message)

        cv2.putText(frame, message, (10, self._frame_height - 20), self.font, 0.5, (255, 255, 255), 2)

        self.last_time = time.time()

        return frame

    def handle_the_people(self, frame):

        (rects, weight) = self.hog.detectMultiScale(frame, winStride=(self.hog_win_stride, self.hog_win_stride),
                                                    padding=(self.hog_padding, self.hog_padding), scale=self.hog_scale)

        people = self.finder.people(rects)

        # draw triplines
        for line in self.lines:
            for person in people:
                if line.handle_collision(person) == 1:
                    self.new_collision(person)
                else:
                    conn.sendall(b'2')
                
            frame = line.draw(frame)

        for person in people:
            frame = person.draw(frame)
            person.colliding = False
        return frame

    def new_collision(self, person):

        if self.endpoint is not None:
            post = {
                'name': person.name,
                'meta': json.dumps(person.meta),
                'date': time.time()
            }

            request = grequests.post(self.endpoint, data=post)
            grequests.map([request])

        if not self.to_stdout:
            conn.sendall(b'1')
            print("NEW COLLISION %s HEADING %s" % (person.name, person.meta['line-0']))
            

