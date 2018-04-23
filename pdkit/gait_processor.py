# Copyright 2018 Birkbeck College. All rights reserved.
#
# Licensed under the MIT license. See file LICENSE for details.
#
# Author: Cosmin Stamate 

import sys
import traceback

import numpy as np
import pandas as pd

from scipy import interpolate, signal, fft
from pywt import wavedec

from .processor import Processor
from .gait_time_series import GaitTimeSeries

from .utils import (load_data,
                    numerical_integration, 
                    autocorrelation,
                    peakdet,
                    compute_interpeak,
                    butter_lowpass_filter,
                    crossings_nonzero_pos2neg,
                    autocorrelate)


class GaitProcessor(Processor):
    '''
       This is the main Gait Processor class. Once the data is loaded it will be
       accessible at data_frame, where it looks like:
       data_frame.x, data_frame.y, data_frame.z: x, y, z components of the acceleration
       data_frame.index is the datetime-like index
       
       This values are recommended by the author of the pilot study [1] and [3]
       
       step_size = 50.0
       start_end_offset = [100, 100]
       delta = 0.5
       loco_band = [0.5, 3]
       freeze_band = [3, 8]
       sampling_frequency = 100.0Hz
       cutoff_frequency = 2.0Hz
       filter_order = 2
       window = 256
       lower_frequency = 2.0Hz
       upper_frequency = 10.0Hz

       [1] Developing a tool for remote digital assessment of Parkinson s disease
            Kassavetis	P,	Saifee	TA,	Roussos	G,	Drougas	L,	Kojovic	M,	Rothwell	JC,	Edwards	MJ,	Bhatia	KP
            
       [2] The use of the fast Fourier transform for the estimation of power spectra: A method based 
            on time averaging over short, modified periodograms (IEEE Trans. Audio Electroacoust. 
            vol. 15, pp. 70-73, 1967)
            P. Welch

       [3] M. Bachlin et al., "Wearable Assistant for Parkinson’s Disease Patients With the Freezing of Gait Symptom,"
           in IEEE Transactions on Information Technology in Biomedicine, vol. 14, no. 2, pp. 436-446, March 2010.
    '''

    def __init__(self,
                 sampling_frequency=100.0,
                 cutoff_frequency=2.0,
                 filter_order=2,
                 window=256,
                 lower_frequency=2.0,
                 upper_frequency=10.0,
                 step_size=50.0,
                 start_end_offset=[100, 100],
                 delta=0.5,
                 loco_band=[0.5, 3],
                 freeze_band=[3, 8],
                 stride_fraction=1.0/8.0,
                 order=4,
                 threshold=0.5,
                 distance=90,
                 step_period=2,
                 stride_period=1):

        super().__init__(sampling_frequency,
                         cutoff_frequency,
                         filter_order,
                         window,
                         lower_frequency,
                         upper_frequency)

        self.step_size = step_size
        self.start_end_offset = start_end_offset
        self.delta = delta
        self.loco_band = loco_band
        self.freeze_band = freeze_band
        self.stride_fraction = stride_fraction
        self.order = order
        self.threshold = threshold
        self.distance = distance
        self.step_period = step_period
        self.stride_period = stride_period


    def freeze_of_gait(self, data_frame):
        ''' 
            This method assess freeze of gait following [3].

            :param DataFrame data_frame: the data frame.
            :return [list, list, list]: The returns are [freeze_times, freeze_indexes, locomotion_freezes].
        '''
        
        # the sampling frequency was recommended by the author of the pilot study
        data = self.resample_signal(data_frame) 
        data = data.y.values

        f_res = self.sampling_frequency / self.window
        f_nr_LBs = int(self.loco_band[0] / f_res)
        f_nr_LBe = int(self.loco_band[1] / f_res)
        f_nr_FBs = int(self.freeze_band[0] / f_res)
        f_nr_FBe = int(self.freeze_band[1] / f_res)

        jPos = self.window + 1
        i = 0
        
        time = []
        sumLocoFreeze = []
        freezeIndex = []
        
        while jPos < len(data):
            
            jStart = jPos - self.window
            time.append(jPos)

            y = data[int(jStart):int(jPos)]
            y = y - np.mean(y)

            Y = np.fft.fft(y, int(self.window))
            Pyy = abs(Y*Y) / self.window #conjugate(Y) * Y / NFFT

            areaLocoBand = numerical_integration( Pyy[f_nr_LBs-1 : f_nr_LBe], self.sampling_frequency )
            areaFreezeBand = numerical_integration( Pyy[f_nr_FBs-1 : f_nr_FBe], self.sampling_frequency )

            sumLocoFreeze.append(areaFreezeBand + areaLocoBand)

            freezeIndex.append(areaFreezeBand / areaLocoBand)

            jPos = jPos + self.step_size
            i = i + 1

        freeze_times = time
        freeze_indexes = freezeIndex
        locomotion_freezes = sumLocoFreeze

        return [freeze_times, freeze_indexes, locomotion_freezes]

    def frequency_of_peaks(self, data_frame):
        ''' 
            This method assess the frequency of the peaks on the x-axis.

            :param DataFrame data_frame: the data frame.
            :return float: The frequency of peaks on the x-axis.
        '''

        peaks_data = data_frame[self.start_end_offset[0]: -self.start_end_offset[1]].x.values
        maxtab, mintab = peakdet(peaks_data, self.delta)
        x = np.mean(peaks_data[maxtab[1:,0].astype(int)] - peaks_data[maxtab[:-1,0].astype(int)])
        frequency_of_peaks = 1/x

        return frequency_of_peaks
        
    def speed_of_gait(self, data_frame, wavelet_type='db3', wavelet_level=6):
        ''' 
            This method assess the speed of gait following [2].
            It extracts the gait speed from the energies of the approximation coefficients of wavelet functions.

            :param DataFrame data_frame: the data frame.
            :param str wavelet_type: the type of wavelet to use. See https://pywavelets.readthedocs.io/en/latest/ref/wavelets.html for a full list.
            :param int wavelet_level: the number of cycles the used wavelet should have. See https://pywavelets.readthedocs.io/en/latest/ref/wavelets.html for a fill list.
            :return float: The speed of gait.
        '''

        coeffs = wavedec(data_frame.mag_sum_acc, wavelet=wavelet_type, level=wavelet_level)

        energy = [sum(coeffs[wavelet_level - i]**2) / len(coeffs[wavelet_level - i]) for i in range(wavelet_level)]

        WEd1 = energy[0] / (5 * np.sqrt(2))
        WEd2 = energy[1] / (4 * np.sqrt(2))
        WEd3 = energy[2] / (3 * np.sqrt(2))
        WEd4 = energy[3] / (2 * np.sqrt(2))
        WEd5 = energy[4] / np.sqrt(2)
        WEd6 = energy[5] / np.sqrt(2)

        gait_speed = 0.5 * np.sqrt(WEd1+(WEd2/2)+(WEd3/3)+(WEd4/4)+(WEd5/5))

        return gait_speed


    def walk_regularity_symmetry(self, data_frame):
        ''' 
            This method extracts the step and stride regularity and also walk symmetry.

            :param DataFrame data_frame: the data frame.
            :return [list, list, list]: The returns are [step_regularity, stride_regularity, walk_symmetry] and each list consists of [x, y, z].
        '''
        
        def _symmetry(v):
            maxtab, _ = peakdet(v, self.delta)
            return maxtab[1][1], maxtab[2][1]

        step_regularity_x, stride_regularity_x = _symmetry(autocorrelation(data_frame.x))
        step_regularity_y, stride_regularity_y = _symmetry(autocorrelation(data_frame.y))
        step_regularity_z, stride_regularity_z = _symmetry(autocorrelation(data_frame.z))

        symmetry_x = stride_regularity_x - step_regularity_x
        symmetry_y = stride_regularity_y - step_regularity_y
        symmetry_z = stride_regularity_z - step_regularity_z

        step_regularity = [step_regularity_x, step_regularity_y, step_regularity_z]
        stride_regularity = [stride_regularity_x, stride_regularity_y, stride_regularity_z]
        walk_symmetry = [symmetry_x, symmetry_y, symmetry_z]

        return [step_regularity, stride_regularity, walk_symmetry]


def walk_direction_preheel(self, data_frame):

    # Sum of absolute values across accelerometer axes:
    data = np.abs(data_frame.x) + np.abs(data_frame.y) + np.abs(data_frame.z)

    # Find maximum peaks of smoothed data:
    dummy, ipeaks_smooth = self.heel_strikes(data_frame)

    # Compute number of samples between peaks using the real part of the FFT:
    interpeak = compute_interpeak(data, self.sampling_frequency)
    decel = np.int(np.round(self.stride_fraction * interpeak))

    # Find maximum peaks close to maximum peaks of smoothed data:
    ipeaks = []
    for ipeak_smooth in ipeaks_smooth:
        ipeak = np.argmax(data[ipeak_smooth - decel:ipeak_smooth + decel])
        ipeak += ipeak_smooth - decel
        ipeaks.append(ipeak)

    # Compute the average vector for each deceleration phase:
    vectors = []
    for ipeak in ipeaks:
        decel_vectors = np.asarray([[data_frame.x[i], data_frame.y[i], data_frame.z[i]]
                                    for i in range(ipeak - decel, ipeak)])
        vectors.append(np.mean(decel_vectors, axis=0))

    # Compute the average deceleration vector and take the opposite direction:
    direction = -1 * np.mean(vectors, axis=0)

    # Return the unit vector in this direction:
    direction /= np.sqrt(direction.dot(direction))

    return direction


def heel_strikes(self, data_frame):

    # Demean data:
    data = np.abs(data_frame.x) + np.abs(data_frame.y) + np.abs(data_frame.z)
    data -= np.mean(data)

    # Low-pass filter the AP accelerometer data by the 4th order zero lag
    # Butterworth filter whose cut frequency is set to 5 Hz:
    filtered = butter_lowpass_filter(data, self.sampling_frequency, self.cutoff_frequency, self.order)

    # Find transitional positions where AP accelerometer changes from
    # positive to negative.
    transitions = crossings_nonzero_pos2neg(filtered)

    # Find the peaks of AP acceleration preceding the transitional positions,
    # and greater than the product of a threshold and the maximum value of
    # the AP acceleration:
    strike_indices_smooth = []
    filter_threshold = np.abs(self.threshold * np.max(filtered))
    for i in range(1, np.size(transitions)):
        segment = range(transitions[i-1], transitions[i])
        imax = np.argmax(filtered[segment])
        if filtered[segment[imax]] > filter_threshold:
            strike_indices_smooth.append(segment[imax])

    # Compute number of samples between peaks using the real part of the FFT:
    interpeak = compute_interpeak(data, self.sampling_frequency)
    decel = np.int(interpeak / 2)

    # Find maximum peaks close to maximum peaks of smoothed data:
    strike_indices = []
    for ismooth in strike_indices_smooth:
        istrike = np.argmax(data[ismooth - decel:ismooth + decel])
        istrike = istrike + ismooth - decel
        strike_indices.append(istrike)

    strikes = np.asarray(strike_indices)
    strikes -= strikes[0]
    strikes = strikes / self.sampling_frequency

    return strikes, strike_indices

def gait_regularity_symmetry(self, data, step_period, stride_period):

    coefficients, N = autocorrelate(data, unbias=2, normalize=2)

    step_regularity = coefficients[step_period]
    stride_regularity = coefficients[stride_period]
    symmetry = np.abs(stride_regularity - step_regularity)

    return step_regularity, stride_regularity, symmetry


def gait(self, data_frame):
    """
    Extract gait features from estimated heel strikes and accelerometer data.
    This function extracts all of iGAIT's features
    that depend on the estimate of heel strikes::
        - cadence = number of steps divided by walk time
        - step/stride regularity
        - step/stride symmetry
        - mean step/stride length and velocity (if distance supplied)
    Parameters
    ----------
    strikes : numpy array
        heel strike timings
    data : list or numpy array
        accelerometer data along forward axis
    duration : float
        duration of accelerometer reading (s)
    distance : float
        distance traversed
    Returns
    -------
    number_of_steps : integer
        estimated number of steps based on heel strikes
    velocity : float
        velocity (if distance)
    avg_step_length : float
        average step length (if distance)
    avg_stride_length : float
        average stride length (if distance)
    cadence : float
        number of steps divided by duration
    step_durations : numpy array
        step durations
    avg_step_duration : float
        average step duration
    sd_step_durations : float
        standard deviation of step durations
    strides : list of two lists of floats
        stride timings for each side
    avg_number_of_strides : float
        estimated number of strides based on alternating heel strikes
    stride_durations : list of two lists of floats
        estimated stride durations
    avg_stride_duration : float
        average stride duration
    sd_step_durations : float
        standard deviation of stride durations
    step_regularity : float
        measure of step regularity along axis
    stride_regularity : float
        measure of stride regularity along axis
    symmetry : float
        measure of gait symmetry along axis
    Examples
    --------
    >>> from mhealthx.xio import read_accel_json, compute_sample_rate
    >>> input_file = '/Users/arno/DriveWork/mhealthx/mpower_sample_data/deviceMotion_walking_outbound.json.items-a2ab9333-6d63-4676-977a-08591a5d837f5221783798792869048.tmp'
    >>> device_motion = True
    >>> start = 150
    >>> t, axyz, gxyz, uxyz, rxyz, sample_rate, duration = read_accel_json(input_file, start, device_motion)
    >>> ax, ay, az = axyz
    >>> from mhealthx.extractors.pyGait import heel_strikes
    >>> threshold = 0.2
    >>> order = 4
    >>> cutoff = 5
    >>> data = ay
    >>> plot_test = False
    >>> strikes, strike_indices = heel_strikes(data, sample_rate, threshold, order, cutoff, plot_test)
    >>> from mhealthx.extractors.pyGait import gait
    >>> distance = 90
    >>> a = gait(strikes, data, duration, distance)
    """

    strikes, strike_indices = self.heel_strikes(data_frame)

    step_durations = []
    for i in range(1, np.size(strikes)):
        step_durations.append(strikes[i] - strikes[i-1])

    avg_step_duration = np.mean(step_durations)
    sd_step_durations = np.std(step_durations)

    number_of_steps = np.size(strikes)
    cadence = number_of_steps / self.duration

    strides1 = strikes[0::2]
    strides2 = strikes[1::2]
    stride_durations1 = []
    for i in range(1, np.size(strides1)):
        stride_durations1.append(strides1[i] - strides1[i-1])
    stride_durations2 = []
    for i in range(1, np.size(strides2)):
        stride_durations2.append(strides2[i] - strides2[i-1])

    strides = [strides1, strides2]
    stride_durations = [stride_durations1, stride_durations2]

    avg_number_of_strides = np.mean([np.size(strides1), np.size(strides2)])
    avg_stride_duration = np.mean((np.mean(stride_durations1),
                                   np.mean(stride_durations2)))
    sd_stride_durations = np.mean((np.std(stride_durations1),
                                   np.std(stride_durations2)))

    step_period = 1 / avg_step_duration
    stride_period = 1 / avg_stride_duration

    step_regularity, stride_regularity, symmetry = self.gait_regularity_symmetry(data, step_period, stride_period)

    # Set distance-based measures to None if distance not set:
    if self.distance:
        velocity = self.distance / self.duration
        avg_step_length = number_of_steps / self.distance
        avg_stride_length = avg_number_of_strides / self.distance
    else:
        velocity = None
        avg_step_length = None
        avg_stride_length = None

    return number_of_steps, cadence, velocity, \
        avg_step_length, avg_stride_length, step_durations, \
        avg_step_duration, sd_step_durations, strides, stride_durations, \
        avg_number_of_strides, avg_stride_duration, sd_stride_durations, \
        step_regularity, stride_regularity, symmetry
