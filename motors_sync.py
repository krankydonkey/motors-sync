# Motors synchronization
#
# Copyright (C) 2024  Maksim Bolgov <maksim8024@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging, time, itertools
import numpy as np
from . import adxl345

DATA_FOLDER = '/tmp'        # Folder where csv are generate
CSV_DELAY = 0.10            # Delay between checks csv in /tmp in sec
CSV_OPEN_DELAY = 0.10       # Delay between open csv in sec
EXIT_TIMER = 5.00           # Exit program time in sec if no file
MEDIAN_FILTER_WINDOW = 3    # Number of window lines
AXES_LEVEL_DELTA = 2000     # Magnitude difference between axes

class MotorsSync:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode_move = self.printer.load_object(config, 'gcode_move')
        self.force_move = self.printer.load_object(config, 'force_move')
        self.stepper_en = self.printer.load_object(config, 'stepper_enable')
        self.printer.register_event_handler("klippy:connect", self.handler)
        # Read config
        self.accel_chip = self.config.get('accel_chip', (self.config.getsection('resonance_tester').get('accel_chip')))
        self.microsteps = self.config.getint('microsteps', default=16, minval=2, maxval=32)
        self.steps_threshold = self.config.getint('steps_threshold', default=1000000, minval=5000, maxval=100000)
        self.fast_threshold = self.config.getint('fast_threshold', default=None, minval=0, maxval=100000)
        self.retry_tolerance = self.config.getint('retry_tolerance', default=None, minval=0, maxval=100000)
        self.max_retries = self.config.getint('retries', default=0, minval=0, maxval=10)
        self.respond = self.config.getboolean('respond', default=True)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('SYNC_MOTORS', self.cmd_RUN_SYNC, desc='Start 4WD synchronization')
        # Variables
        self.move_len = 40 / 200 / self.microsteps

    def handler(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.travel_speed = self.toolhead.max_velocity / 2

    def _send(self, func):
        self.gcode._process_commands([func], False)

    def _stepper_switch(self, stepper, mode):
        self.stepper_en.motor_debug_enable(stepper, mode)

    def _stepper_move(self, stepper, dist):
        self.force_move.manual_move(stepper, dist, 100, 5000)

    def _static_measure(self):
        # Measure static vibrations
        self._send(f'ACCELEROMETER_MEASURE CHIP={self.accel_chip} NAME=stand_still')
        time.sleep(0.25)
        self._send(f'ACCELEROMETER_MEASURE CHIP={self.accel_chip} NAME=stand_still')
        # Init CSV file
        file = self._wait_csv('stand_still.csv')
        vect = np.mean(np.genfromtxt(file, delimiter=',', skip_header=1, usecols=(1, 2, 3)), axis=0)
        os.remove(file)
        # Calculate static and find z axis for future exclude
        self.z_axis = np.abs(vect[0:]).argmax()
        xy_vect = np.delete(vect, self.z_axis, axis=0)
        self.static_data = round(np.linalg.norm(xy_vect, axis=0), 2)

    def _wait_csv(self, name):
        # Wait csv data file in data folder
        timer = 0
        while True:
            time.sleep(CSV_DELAY)
            timer += 1
            for f in os.listdir(DATA_FOLDER):
                if f.endswith(name):
                    time.sleep(CSV_OPEN_DELAY)
                    return os.path.join(DATA_FOLDER, f)
                elif timer > EXIT_TIMER / CSV_DELAY:
                    raise self.gcode.error(f'No CSV files found in the directory, aborting')
                else: continue

    def _buzz(self, stepper):
        # Fading oscillations
        lookup_sec_stepper = self.force_move._lookup_stepper({'STEPPER': stepper + '1'})
        self._stepper_switch(stepper, 0)
        for i in range(0, int(self.microsteps * 2.5)):
            dist = (1 - self.move_len * 2 * i) * 2
            self._stepper_move(lookup_sec_stepper, dist)
            self._stepper_move(lookup_sec_stepper, -dist)
        self._stepper_switch(stepper, 1)

    def _calc_magnitude(self):
        try:
            # Init CSV file
            file = self._wait_csv('.csv')
            vect = np.genfromtxt(file, delimiter=',', skip_header=1, usecols=range(1,4))
            os.remove(file)
            xy_vect = np.delete(vect, self.z_axis, axis=1)
            # Add window mean filter
            magnitude = []
            for i in range(int(MEDIAN_FILTER_WINDOW / 2), len(xy_vect) - int(MEDIAN_FILTER_WINDOW / 2)):
                filtered_xy_vect = (np.median([xy_vect[i-1], xy_vect[i], xy_vect[i+1]], axis=0))
                magnitude.append(np.linalg.norm(filtered_xy_vect))
            # Return avg of 5 max magnitudes with deduction static
            magnitude = np.mean(np.sort(magnitude)[-5:])
            return round(magnitude - self.static_data, 2)
        except Exception as e:
            self.gcode.error(f"Error processing generated CSV: {str(e)}")

    def _measure(self, stepper, buzz=True):
        if buzz: self._buzz(stepper)
        self._stepper_switch(stepper, 0)
        time.sleep(0.25)
        self._send(f'ACCELEROMETER_MEASURE CHIP={self.accel_chip}')
        self._stepper_switch(stepper, 1)
        self._send(f'ACCELEROMETER_MEASURE CHIP={self.accel_chip}')
        return self._calc_magnitude()

    def _prestart(self):
        os.system(f'rm -f {DATA_FOLDER}/*.csv')
        now = self.printer.get_reactor().monotonic()
        kin_status = self.toolhead.get_kinematics().get_status(now)
        self.center_x = (int(self.config.getsection('stepper_x').get('position_max'))
                         - int(self.config.getsection('stepper_x').get('position_min'))) / 2
        self.center_y = (int(self.config.getsection('stepper_y').get('position_max'))
                         - int(self.config.getsection('stepper_y').get('position_min'))) / 2
        if 'xy' not in kin_status['homed_axes']:
            self._send('G28 X Y')
        self.toolhead.manual_move([self.center_x,self.center_y , None], self.travel_speed)
        self.toolhead.wait_moves()

    def _axes_level(self):
        # Axes leveling by magnitude
        # main_axis = self.motion['max_axis']
        # sec_axis = self.motion['min_axis']
        max_magnitude = self.motion[self.motion['max_axis']]['new_magnitude']
        min_magnitude = self.motion[self.motion['min_axis']]['init_magnitude']
        delta = round(max_magnitude - min_magnitude, 2)
        if delta > AXES_LEVEL_DELTA:
            self.gcode.respond_info(f'Start axes level, delta: {delta}')
            m = self.motion[self.motion['max_axis']]
            while True:
                buzz = False if m['magnitude'] > self.fast_threshold and self.fast_threshold else True
                m['moving_msteps'] = max(int((m['magnitude'] - self.motion[self.motion['min_axis']]['magnitude']) / self.steps_threshold), 1)
                self._stepper_move(m['lookup_stepper'], m['moving_msteps'] * self.move_len * m['move_dir'][0])
                m['new_magnitude'] = self._measure(m['stepper'], buzz)
                if self.respond: self.gcode.respond_info(
                    f"{self.motion['max_axis'].upper()}-New magnitude: {m['new_magnitude']} on "
                    f"{m['move_dir'][0] * m['moving_msteps']}/{self.microsteps} step move")
                if m['new_magnitude'] > m['magnitude']:
                    raise self.gcode.error('Fatal error in loop! Data is incorrect')
                delta = round(m['new_magnitude'] - min_magnitude, 2)
                if delta < AXES_LEVEL_DELTA or m['new_magnitude'] < min_magnitude:
                    m['magnitude'] = m['new_magnitude']
                    self.gcode.respond_info(
                        f"Axes are leveled: {self.motion['max_axis'].upper()}: {max_magnitude} --> "
                        f"{m['new_magnitude']} {self.motion['min_axis'].upper()}: {min_magnitude}, delta: {delta}")
                    break
                m['magnitude'] = m['new_magnitude']

    def _detect_move_dir(self, axis):
        # Determine movement direction
        self._stepper_move(self.motion[axis]['lookup_stepper'], self.motion[axis]['moving_msteps'] * self.move_len)
        self.motion[axis]['actual_msteps'] += self.motion[axis]['moving_msteps']
        self.motion[axis]['new_magnitude'] = self._measure(self.motion[axis]['stepper'], True)
        if self.respond: self.gcode.respond_info(
            f"{axis.upper()}-New magnitude: {self.motion[axis]['new_magnitude']}"
            f" on {self.motion[axis]['move_dir'][0] * self.motion[axis]['moving_msteps']}/{self.microsteps} step move")
        self.motion[axis]['move_dir'] = [-1, 'Backward'] if (self.motion[axis]['new_magnitude']
                                                             > self.motion[axis]['magnitude']) else [1, 'Forward']
        if self.respond: self.gcode.respond_info(f"{axis.upper()}-Movement direction: {self.motion[axis]['move_dir'][1]}")
        self.motion[axis]['magnitude'] = self.motion[axis]['new_magnitude']

    def _final_sync(self, axes):
        # Axes calibration to zero magnitude
        self.gcode.respond_info(f'Final stage of sync')
        if self.motion['min_axis'] == axes[-1]: axes.reverse()
        for axis in itertools.cycle(axes):
            m = self.motion[axis]
            if not m['out']:
                if not m['move_dir'][1]: self._detect_move_dir(axis)
                buzz = False if m['magnitude'] > self.fast_threshold and self.fast_threshold else True
                m['moving_msteps'] = max(int(m['magnitude'] / self.steps_threshold), 1)
                self._stepper_move(m['lookup_stepper'], m['moving_msteps'] * self.move_len * m['move_dir'][0])
                m['actual_msteps'] += m['moving_msteps']
                m['new_magnitude'] = self._measure(m['stepper'], buzz)
                if self.respond: self.gcode.respond_info(
                    f"{axis.upper()}-New magnitude: {m['new_magnitude']}"
                    f" on {m['move_dir'][0] * m['moving_msteps']}/{self.microsteps} step move")
                if m['new_magnitude'] > m['magnitude']:
                    self._stepper_move(m['lookup_stepper'], m['moving_msteps'] * self.move_len * m['move_dir'][0] * -1)
                    m['actual_msteps'] -= m['moving_msteps'] * m['move_dir'][0]
                    if self.retry_tolerance and m['magnitude'] > self.retry_tolerance:
                        m['retries'] += 1
                        if m['retries'] > self.max_retries:
                            self.gcode.respond_info(
                                f"{axis.upper()} Motors adjusted by {m['actual_msteps']}/{self.microsteps}"
                                f" step, magnitude {m['init_magnitude']} --> {m['magnitude']}")
                            raise self.gcode.error('Too many retries')
                        if self.respond: self.gcode.respond_info(
                            f"{axis.upper()} Retries: {m['retries']}/{self.max_retries} Back on last magnitude:"
                            f" {m['magnitude']} on {m['actual_msteps']}/{self.microsteps} step to reach {self.retry_tolerance}")
                        m['move_dir'][1] = 0
                        continue
                    m['out'] = (f"{axis.upper()} Motors adjusted by {m['actual_msteps']}/"
                                f"{self.microsteps} step, magnitude {m['init_magnitude']} --> {m['magnitude']}")
                    continue
                m['magnitude'] = m['new_magnitude']
            else:
                if self.motion[axes[0]]['out'] and self.motion[axes[1]]['out']: break

    def cmd_RUN_SYNC(self, gcmd):
        # Live variables
        self.axes = ['x', 'y']
        self.accel_chip = gcmd.get('ACCEL_CHIP', self.accel_chip)
        self.steps_threshold = gcmd.get_int('STEPS_THRESHOLD', self.steps_threshold, minval=5000, maxval=100000)
        self.fast_threshold = gcmd.get_int('FAST_THRESHOLD', self.fast_threshold, minval=0, maxval=100000)
        self.retry_tolerance = gcmd.get_int('RETRY_TOLERANCE', self.retry_tolerance, minval=0, maxval=100000)
        self.max_retries = gcmd.get_int('RETRIES', self.max_retries, minval=0, maxval=10)
        # Run
        self._prestart()
        self._static_measure()
        self.motion = {}
        if self.respond: self.gcode.respond_info('Motors synchronization started')
        axes = ['x', 'y']
        # Init axes
        for axis in axes:
            self.motion[axis] = {}
            self.motion[axis]['stepper'] = 'stepper_' + axis.lower()
            self.motion[axis]['lookup_stepper'] = self.force_move._lookup_stepper({'STEPPER': 'stepper_' + axis.lower()})
            self.motion[axis]['init_magnitude'] = self._measure(self.motion[axis]['stepper'], True)
            self.motion[axis]['magnitude'] = self.motion[axis]['init_magnitude']
            self.motion[axis]['new_magnitude'] = 0
            self.motion[axis]['move_dir'] = [1, 'Forward']
            self.motion[axis]['moving_msteps'] = max(int(self.motion[axis]['magnitude'] / self.steps_threshold), 1)
            self.motion[axis]['actual_msteps'] = 0
            self.motion[axis]['retries'] = 0
            self.motion[axis]['out'] = 0
            if self.respond: self.gcode.respond_info(f"{axis.upper()}-Initial magnitude: {self.motion[axis]['init_magnitude']}")
        self.motion['max_axis'] = max(self.motion, key=lambda level: self.motion[level]['init_magnitude'] if 'init_magnitude' in self.motion[level] else float('-inf'))
        self.motion['min_axis'] = min(self.motion, key=lambda level: self.motion[level]['init_magnitude'] if 'init_magnitude' in self.motion[level] else float('inf'))
        self._detect_move_dir(self.motion['max_axis'])
        self.motion[self.motion['min_axis']]['move_dir'][1] = 0
        self._axes_level()
        self.motion[self.motion['min_axis']]['magnitude'] = self._measure(self.motion[self.motion['min_axis']]['stepper'], True)
        if self.respond: self.gcode.respond_info(
            f"{self.motion['min_axis'].upper()}-New magnitude: {self.motion[self.motion['min_axis']]['magnitude']}")
        self._final_sync(axes)
        # Info
        for axis in axes:
            self.gcode.respond_info(f"{self.motion[axis]['out']}\n")

def load_config(config):
    return MotorsSync(config)