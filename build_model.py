from comet_ml import ConfusionMatrix, Experiment

import cv2
import os
import multiprocessing
import time
import datetime

from collections import Counter, OrderedDict
from pathlib import Path

import librosa
import librosa.display
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from keras.callbacks import Callback, EarlyStopping
from keras.layers import BatchNormalization
from keras.layers.convolutional import MaxPooling2D, Conv2D
from keras.layers.core import Dense, Dropout, Flatten
from keras.models import load_model, Sequential
from keras.preprocessing.image import ImageDataGenerator
from keras.utils.np_utils import to_categorical
from loguru import logger
from sklearn.decomposition import PCA
from sklearn.feature_selection import SelectKBest
from sklearn.feature_selection import f_classif
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

import constants

from torchHHT import hht, visualization
from functools import partial

from create_file_data_csv import create_file_data_csv


# To use Comet ML visualization and logging you have to follow the instructions from README.md
# on how to set COMET_API_KEY, COMET_WORKSPACE, COMET_PROJECT_NAME environment variables
# Alternatively, you can set these variables manually in the code here by uncommenting the lines below
os.environ["COMET_API_KEY"] = 'hMtnaNF5Fdgy1BQdb1sCb0MEX'
os.environ["COMET_WORKSPACE"] = 'carnivorousmole'
COMET_PROJECT_NAME= str(datetime.datetime.now().day)+"-"+datetime.datetime.now().strftime("%b")

USE_COMET_ML = True

# Creating an experiment in the cloud
def create_experiment():
    if USE_COMET_ML:
        global experiment 
        experiment = Experiment(
            api_key=os.environ["COMET_API_KEY"],
            workspace=os.environ["COMET_WORKSPACE"],
            project_name= COMET_PROJECT_NAME
        )
    logger.debug('Naming COMET expt: ' + EXPT_NAME)
    experiment.set_name(EXPT_NAME)

"""Parameters to adjust"""
# Overwrite Files Option
# TODO: actually implement this level of overwrite control
OVERWRITE_INDIV_FEATURE_FILES = False # If set to true, the model will not use any already features- creating everything from scratch
OVERWRITE_FEATURE_FILES = True # If set to true, the model will not use any already created features sets (ie train test splits)
OVERWRITE_MODEL_FILES = True

SEGMENT_DATA = False # If set to False, the data will not be split into frames at all
PRE_SEGMENT_DATA = True # If set to true, the data will be segmented prior to train test split
# Shortening Clips Option
# SHORTEN_CLIPS = False # Shortens the clips, set to False if the clips have already been manually shortened
NUM_SECONDS = None #the number of seconds of the clip to use, set to None if not using
START_TIME = 0.5 #the number of seconds to start the clip at
NORMALIZE_BY_ROW = False # If set to true, the data will be normalized by row
NUM_CNN_LAYERS = 4 # The number of CNN layers to use
MAX_POOL_4 = False # If set to true, the model will use a max pooling layer after the 3rd and 4th layers

# what languages to use
# LANG_SET = 'en_ar_mn_64mel_' 
LANG_SET = 'en_ar_mn_fr_sp_64mel_' 


FEATURES = 'mfcc'  # mfcc / f0 / cen / rol / chroma / rms / zcr / fbe [Feature types] mfcc_f0_cen_rol_chroma_rms_zcr
MAX_PER_LANG = 150  # maximum number of audios of a language

UNSILENCE = False

WIN_LENGTH_MS = 25  # ms / 25 # time resolution is 
OVERLAP_MS = 10  # ms / 10

SAMPLE_RATE = 22050  # 22050 / 16000 [Hz]
HOP_LENGTH = int(SAMPLE_RATE * 0.001 * OVERLAP_MS)  # [10 ms overlap]
WIN_LENGTH = int(SAMPLE_RATE * 0.001 * WIN_LENGTH_MS)  # [25 ms window length]
# N_FFT = int(SAMPLE_RATE * 0.001 * WIN_LENGTH)  # [25 ms window length]
FRAME_SIZE = 75  # 30 / 50 / 70 / 100 / 150 / 200 / 300 / 500 [Size of feature segment]

MEL_S_LOG = True
HIL_S_LOG = True

selection_method = 'UNIVARIATE'  # PCE / UNIVARIATE
SCORE_FUNC = f_classif  # f_classif / mutual_info_classif [score function for univariate  feature selector]
NUM_OF_FEATURES = 10  # [number of optimal features to work with]
SELECT_FEATURES = False  # [whether to use feature selection method]
CHECK_DATASETS = False
FILTER_INPUT_DATA = True  # [whether to filter the input data to only samples in filtered_filenames.txt]

EPOCHS = 500  # [Number of training epochs]
BATCH_SIZE = 64  # size of mini-batch used
KERNEL_SIZE = (3, 3)  # (3, 3) (5, 5)
POOL_SIZE = (2, 2)  # (2, 2) (3, 3)
DROPOUT = 0.1  # 0.5 for mfcc CNN
BASELINE = 1.0
MIN_DELTA = .005  # .01
PATIENCE = EPOCHS  # 10 - set to epochs to temporarily disable early stopping as it was resulting in lower final accuracy
N_MELS = 64  # [number of filters for a mel-spectrogram]

EXPT_NAME = LANG_SET+FEATURES+"_"+str(datetime.datetime.now().hour)+str(datetime.datetime.now().minute) +"_"+str(datetime.datetime.now().day)+str(datetime.datetime.now().month)

AUDIO_INPUT_PATH = constants.AUDIO_INPUT_PATH

def pad_to_max_shape(arr_list):
    max_shape = max([x.shape for x in arr_list])
    padded_arr_list = []
    for arr in arr_list:
        pad_width = [(0, max_shape[i] - arr.shape[i]) for i in range(len(max_shape))]
        padded_arr = np.pad(arr, pad_width=pad_width, mode='constant')
        padded_arr_list.append(padded_arr)
    return padded_arr_list

def filter_df(df):
    """
    Filters audio files DataFrame based on options:
    [language, path -- path to file, path_unsilenced -- path to file with removed silence parts].
    Dictionary of available languages is defined in constants.py.
    :param df: (DataFrame) unfiltered audio files DataFrame
    :return: (DataFrame) filtered DataFrame
    """

    lang_codes = [lc for lc in LANG_SET.split('_') if lc in constants.LANGUAGES]
    df_to_include = []
    for lang_code in lang_codes:
        lang_fullname = constants.LANGUAGES[lang_code]
        # TODO: Filter recordings randomly (based on random seed), not first ones
        df_to_include.append(df[df.language == lang_fullname][:MAX_PER_LANG])
    return pd.concat(df_to_include)

def trim_sound(y, sr, start, n_seconds):
    samples = sr * n_seconds
    start_sample = int(sr * start)
    trimmed_sound = y[start_sample:start_sample+samples]
    return trimmed_sound

def extract_features(audio_file,features_string):
    """
    Extracts features from audio files.
    Different kinds of features are concatenated subsequently.
    :param audio_file: (String) path to a .wav audio file
    features_string: (String) string of features to extract
    :return: (numpy.ndarray) feature matrices
    (columns == FRAME_SIZE, rows == number of features)
    """
    features_dir = constants.SAVED_FEATURES_PATH +  AUDIO_INPUT_PATH.split('/')[-1]
    saved_filepath = features_dir +"/"+ features_string +"_" +os.path.basename(audio_file.replace('.wav','.npy'))
    Path(features_dir).mkdir(parents=True, exist_ok=True)

    if OVERWRITE_FEATURE_FILES or not Path(saved_filepath).exists():

        if not Path(audio_file).exists():
            logger.warning(f"Audio file {audio_file} is not found. Check the dataset")
            return
        y, sr = librosa.load(audio_file, sr=None)
        y = librosa.core.resample(y=y, orig_sr=sr, target_sr=SAMPLE_RATE, scale=True) #resample at defined SAMPLE_RATE
        s, _ = librosa.magphase(librosa.stft(y, hop_length=HOP_LENGTH, win_length=WIN_LENGTH))  # magnitudes of spectrogram

        if(NUM_SECONDS):
            y = trim_sound(y,SAMPLE_RATE,START_TIME,NUM_SECONDS) # shorten the length of the clip

        features = []
        if 'mfcc' in features_string:
            mfccs = derive_mfcc(audio_file, y)
            features.append(mfccs)
        if 'mhfcc' in features_string:
            mhfccs = derive_mhfcc(audio_file, y)
            features.append(mhfccs)
        if 'f0' in features_string:
            f0 = derive_f0(audio_file, y)
        if 'cen' in features_string:
            spectral_centroid = derive_spectral_centroid(audio_file, y)
            features.append(spectral_centroid)
        if 'rol' in features_string:
            spectral_rolloff = derive_spectral_rolloff(audio_file, y)
            features.append(spectral_rolloff)
        if 'chroma' in features_string:
            chromagram = derive_chromagram(audio_file, y)
            features.append(chromagram)
        if 'rms' in features_string:
            rms = derive_rms(audio_file, s)
            features.append(rms)
        if 'zcr' in features_string:
            zcr = derive_zcr(audio_file, y)
            features.append(zcr)
        if 'fbe' in features_string:
            mel_s = derive_mel_s(audio_file, y)
            features.append(mel_s)
        if 'hil' in features_string:       
            hil_s = derive_hilbert_s(audio_file, y)
            features.append(hil_s)

        logger.debug('Concatenating extracted features...')
        features = np.vstack(features)
        logger.debug(f'Shape of concatenated features: {features.shape}')
        logger.debug('Saving extracted features to file: '+saved_filepath)
        np.save(saved_filepath, features)
    else:
        logger.debug('Loading extracted features from file: '+saved_filepath)
        features = np.load(saved_filepath)      
    
    return features


def normalize_feature_vectors(feature_vectors):
    """
    Normalizes features presented by a vector (e.g. Mel-Cepstral coefficients, Mel-spectrogram).
    One vector corresponds to an audio segment of WIN_LENGTH length.
    :param feature_vectors: (numpy.ndarray) Vectors of features extracted from an audio file.
    :return: (numpy.ndarray) List of normalized vectors of features
    """
    if NORMALIZE_BY_ROW:
        mean = np.mean(feature_vectors.T, axis=0, dtype=np.float64)
        std = np.std(feature_vectors, dtype=np.float64)
        feature_vectors_normalized = []
        for i in range(feature_vectors.shape[1]):
            feature_vectors_normalized.append(np.subtract(feature_vectors[:, i], mean) / std)
        feature_vectors_normalized = np.array(feature_vectors_normalized)
    else:
        mean = np.mean(feature_vectors, dtype=np.float64)
        std = np.std(feature_vectors, dtype=np.float64)
        feature_vectors_normalized = (feature_vectors.T - mean) / std
    return feature_vectors_normalized.T


def normalize_scalar_feature(feature_vector):
    """
    Normalizes scalar features (e.g. spectral roll-off, F0, etc.).
    Each feature is extracted from an audio segment of WIN_LENGTH length.
    :param feature_vector: (numpy.ndarray) Vector of scalar features
    :return: (numpy.ndarray) List of normalized features
    """
    mean = np.mean(feature_vector, dtype=np.float64)
    std = np.std(feature_vector, dtype=np.float64)
    feature_vector_normalized = (feature_vector - mean) / std
    return feature_vector_normalized

def derive_mhfcc(audio_file, y):
    """
    Derives Mel-Cepstral coefficients from each frame of an audio file - HILBERT VERSION
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vectors of normalized MFCC
    """
    logger.debug(f'Extracting MHFCC for {audio_file}...')

    hil_s = derive_hilbert_s(audio_file, y, LOG_NORM_OVERRIDE = True)

    mhfcc = librosa.feature.mfcc(S=hil_s, sr=SAMPLE_RATE, n_mfcc=13, hop_length=HOP_LENGTH, win_length=WIN_LENGTH)
    mhfcc_normalized = normalize_feature_vectors(mhfcc)
    return mhfcc_normalized


def derive_mfcc(audio_file, y):
    """
    Derives Mel-Cepstral coefficients from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vectors of normalized MFCC
    """
    logger.debug(f'Extracting MFCC for {audio_file}...')
    '''if 'energy' in LANG_SET:
        mfcc = python_speech_features.mfcc(signal=y, samplerate=SAMPLE_RATE, winlen=WIN_LENGTH / SAMPLE_RATE,
                                           winstep=HOP_LENGTH / SAMPLE_RATE, appendEnergy=True, numcep=14,
                                           winfunc=hann, preemph=0.0, ceplifter=0, nfilt=128, lowfreq=0,
                                           highfreq=None, nfft=2048).T
    if 'log' in LANG_SET:
        mel_s = librosa.feature.melspectrogram(y=y, sr=SAMPLE_RATE, n_mels=N_MELS, hop_length=HOP_LENGTH,
                                               win_length=WIN_LENGTH, power=2.0)
        mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel_s), n_mfcc=13, hop_length=HOP_LENGTH,
                                    win_length=WIN_LENGTH)
                                    
    else: '''

    mfcc = librosa.feature.mfcc(y=y, sr=SAMPLE_RATE, n_mfcc=13, hop_length=HOP_LENGTH, win_length=WIN_LENGTH)
    mfcc_normalized = normalize_feature_vectors(mfcc)
    return mfcc_normalized

def downsample(arr, N2):
    N = arr.shape[1]  # width of the array
    M = N // (N2)
    result = np.zeros((arr.shape[0], N2))  # create a new array with the new width
    for i in range(N2):
        result[:, i] = np.mean(arr[:, i*M:(i+1)*M], axis=1)  # take the average of M consecutive elements
        # TODO not sure if should be mean or sum...
    return result

def derive_hilbert_s(audio_file, y,  LOG_NORM_OVERRIDE = False): 
    """
    Derives Hilbert Spectrum
    """
    num_samples = y.shape[0] # number of samples in the audio file
    N = int(num_samples/HOP_LENGTH)+1 # defining the number of frames (used to downsample the result)
    logger.debug(f'Extracting Hilbert-spectrum for {audio_file}...')
    imfs, imfs_env, imfs_freq = hht.hilbert_huang(y, SAMPLE_RATE, num_imf=5)
    # visualization.plot_IMFs(y, imfs, SAMPLE_RATE, save_fig="test_images/emd.png")
    # lowest_imf = 2
    # highest_imf = 5
    hil_s, t, f = hht.hilbert_spectrum(imfs_env, imfs_freq, SAMPLE_RATE, freq_res = 100)

    # visualization.plot_HilbertSpectrum(hil_s, t, f, energy_scale='log',
    #                                 save_spectrum="test_images/Hilbert_spectrum.png", 
    #                                 save_marginal="test_images/Hilbert_marginal.png")
    hil_s = hil_s.numpy().transpose()
    hil_s = downsample(hil_s,N) # downsample to feature vector size

    hil_s = mel_spectrum_transform(hil_s, SAMPLE_RATE ,n_mels=N_MELS, fmin=0, fmax=None)

    # plt.figure(figsize=(10, 5))
    # librosa.display.specshow(hil_s, sr=SAMPLE_RATE, hop_length=HOP_LENGTH, x_axis='time', y_axis='mel', fmin=0, fmax=SAMPLE_RATE/2)
    # plt.colorbar(format='%+2.0f dB')
    # plt.title('Log-magnitude Mel-frequency Spectrogram')
    # plt.tight_layout()
    # plt.show()
    if HIL_S_LOG or LOG_NORM_OVERRIDE:
        hil_s = librosa.power_to_db(hil_s)
    if not LOG_NORM_OVERRIDE:
        hil_s = normalize_feature_vectors(hil_s)
    return hil_s


def mel_spectrum_transform(spectrum, sample_rate, n_mels=128, fmin=0, fmax=None):
    fmax =  sample_rate / 2
    spectrum = spectrum.T
    n_fft = 2*(spectrum.shape[1] - 1)
    mel_basis = librosa.filters.mel(sample_rate, n_fft, n_mels=n_mels, fmin=fmin, fmax=fmax)

    mel_spectrum = np.dot(mel_basis, spectrum.T)

    return mel_spectrum

def derive_mel_s(audio_file, y):
    """
    Derives Mel-Spectrogram of amplitude from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vectors of normalized mel-spectrograms
    """
    logger.debug(f'Extracting Mel-spectrogram for {audio_file}...')
    # print(y.shape[0])
    # print(y.shape," ",HOP_LENGTH," ",WIN_LENGTH)
    mel_s = librosa.feature.melspectrogram(y=y, sr=SAMPLE_RATE, n_mels=N_MELS, hop_length=HOP_LENGTH,
                                           win_length=WIN_LENGTH)

    if MEL_S_LOG:
        mel_s = librosa.power_to_db(mel_s)

    mel_s_normalized = normalize_feature_vectors(mel_s)
    return mel_s_normalized

def derive_f0(audio_file, y):
    """
    Derives fundamental frequencies from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vector of normalized fundamental frequencies
    """
    logger.debug(f'Extracting fundamental frequency for {audio_file}...')
    f0 = librosa.yin(y, librosa.note_to_hz('C2'), librosa.note_to_hz('C7'), sr=SAMPLE_RATE, hop_length=HOP_LENGTH,
                     win_length=WIN_LENGTH)
    f0_normalized = normalize_scalar_feature(f0)
    return f0_normalized

def derive_spectral_centroid(audio_file, y):
    """
    Derives spectral centroid from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vector of normalized spectral centroids
    """
    logger.debug(f'Extracting spectral centroid for {audio_file}...')
    spectral_centroid = librosa.feature.spectral_centroid(y, sr=SAMPLE_RATE, hop_length=HOP_LENGTH,
                                                          win_length=WIN_LENGTH)
    spectral_centroid_normalized = normalize_scalar_feature(spectral_centroid)
    return spectral_centroid_normalized

def derive_spectral_rolloff(audio_file, y):
    """
    Derives spectral centroid from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vector of normalized spectral roll-off values
    """
    logger.debug(f'Extracting spectral rolloff for {audio_file}...')
    rolloff = librosa.feature.spectral_rolloff(y, sr=SAMPLE_RATE, hop_length=HOP_LENGTH, win_length=WIN_LENGTH)
    rolloff_normalized = normalize_scalar_feature(rolloff)
    return rolloff_normalized

def derive_chromagram(audio_file, y):
    """
    Derives N chroma bins from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vectors of normalized chroma bins of an audio file
    """
    logger.debug(f'Extracting chromagram for {audio_file}...')
    chromagram = librosa.feature.chroma_stft(y=y, sr=SAMPLE_RATE, hop_length=HOP_LENGTH, win_length=WIN_LENGTH)
    chromagram_normalized = normalize_feature_vectors(chromagram)
    return chromagram_normalized

def derive_rms(audio_file, s):
    """
    Derives root-mean-square (RMS) value from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param s: (numpy.ndarray) magnitudes (S) of a Spectrogram
    :return: (numpy.ndarray) Vector of normalized RMS values
    """
    logger.debug(f'Extracting chromagram for {audio_file}...')
    rms = librosa.feature.rms(S=s)[0]
    rms_normalized = normalize_scalar_feature(rms)
    return rms_normalized

def derive_zcr(audio_file, y):
    """
    Derives zero-crossing rate from each frame of an audio file.
    Coefficients are normalized for each audio file to deal with
    the difference in volume and background noise.
    :param audio_file: (String) Relative audio file name
    :param y: (numpy.ndarray) Loaded and resampled at SAMPLE_RATE audio file
    :return: (numpy.ndarray) Vector of normalized ZCR
    """
    logger.debug(f'Extracting ZCR for {audio_file}...')
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=HOP_LENGTH, frame_length=WIN_LENGTH * 2)
    zcr_normalized = normalize_scalar_feature(zcr)
    return zcr_normalized

def split_into_matrices(feature_vectors, labels):
    """
    Makes segments of vectors of features
    and attaches them to the corresponding labels.
    :param feature_vectors: vectors of features
    :param labels: list of labels
    :return: (tuple) Matrices with corresponding labels
    """
    segments = []
    seg_labels = []
    for feature_vector, label in zip(feature_vectors, labels):
        for frame_start in range(0, int(feature_vector.shape[1] / FRAME_SIZE)):
            segments.append(feature_vector[:, frame_start * FRAME_SIZE:(frame_start + 1) * FRAME_SIZE])
            seg_labels.append(label)
    return segments, seg_labels

def create_segments_after_selection(data_arrays):
    """
    Splits selected features into matrices
    :param data_arrays:
    :return: matrices of features
    """
    segments_arrays = ()
    for data_array in data_arrays:
        segments = []
        logger.debug(f'\nShape of data before segmenting: {data_array.shape}')
        for element in data_array:
            segments.append(element.reshape(NUM_OF_FEATURES, FRAME_SIZE))
        logger.debug(f'Shape of segmented data: {np.array(segments).shape}\n')
        segments_arrays = segments_arrays + (np.array(segments),)
    return segments_arrays

def preprocess_new_data(x, y):
    """
    Loads .WAV files, extracts features from them and saves extracted features
    with corresponding meta information to files for future use.
    :param x: list of audio paths
    :param y: corresponding languages
    :return: (tuple) train and test sets, information about classes distribution
    """
    logger.info(f'Languages distribution by audios: {Counter(y)}')

    logger.debug('Transforming y to categorical...')
    le = LabelEncoder()
    y_categorical = to_categorical(le.fit_transform(y))

    classes = get_classes_map(y_categorical, y)

    logger.debug('Loading WAV files...')
    pool = multiprocessing.Pool(processes=multiprocessing.cpu_count())

    extract_features_fixed =partial(extract_features, features_string=FEATURES) # fix features argument
    x = pool.map(extract_features_fixed, x)
    # save the first element of x as a png file
    plt.clf()
    plt.imshow(x[0])
    # save the figure with the name of the features extracted
    plt.savefig("test_images/features_test_images/"+FEATURES + ".png")

    if any(feature is None for feature in x):
        logger.error("Some audio files are missing. See the log warnings above and fix the dataset before proceeding")
        return None

    logger.debug('Making segments of feature vectors...')

    # TRAIN TEST SPLIT VARIATIONS
    if(SEGMENT_DATA):
        if(PRE_SEGMENT_DATA):
            # A - segment first then split into train and test
            # print("type of x and y: ", type(x)," ", type(y))
            x_segmented, y_segmented = split_into_matrices(x, y_categorical)
            # print("type of seg x and y: ", type(x_segmented)," ", type(y_segmented))
            x_train, x_test, y_train, y_test = train_test_split(x_segmented, y_segmented, test_size=0.25, random_state=1234)
            # print("type of train x and y: ", type(x_train)," ", type(y_train))
            # print("type of test x and y: ", type(x_test)," ", type(y_test))
            # type of x and y:  <class 'list'>   <class 'pandas.core.series.Series'>
            # type of seg x and y:  <class 'list'>   <class 'list'>
            # type of train x and y:  <class 'list'>   <class 'list'>
            # type of test x and y:  <class 'list'>   <class 'list'>

        else:
            # B - split into train and test then segment
            x_train_initial, x_test_initial, y_train_initial, y_test_initial = train_test_split(x, y_categorical, test_size=0.25, random_state=1234)
            x_train, y_train = split_into_matrices(x_train_initial, y_train_initial)
            x_test, y_test = split_into_matrices(x_test_initial, y_test_initial)
    else:
        same_shape = all([x_element.shape == x[0].shape for x_element in x])
        if not same_shape:
            x = pad_to_max_shape(x)

        x_train, x_test, y_train, y_test = train_test_split(x, y_categorical, test_size=0.25, random_state=1234)
        # print("type of train x and y: ", type(x_train)," ", type(y_train))
        # print("type of test x and y: ", type(x_test)," ", type(y_test))
        # type of train x and y:  <class 'list'>   <class 'numpy.ndarray'>
        # type of test x and y:  <class 'list'>   <class 'numpy.ndarray'>

    logger.debug(f'Train shape: {len(y_train)}')
    train_count = Counter([np.where(y == 1)[0][0] for y in y_train])
    test_count = Counter([np.where(y == 1)[0][0] for y in y_test])

    logger.debug(f'Train count: {train_count}')
    logger.debug(f'Test count: {test_count}')

    logger.debug(f'Length of training set: {len(x_train)}')
    logger.debug(f'Length of testing set: {len(x_test)}')

    assert (len(x_train) == len(y_train)) and (len(x_test) == len(y_test))

    save_input_data_to_files(x_train, x_test, y_train, y_test, train_count, test_count, classes)

    return np.array(x_train), np.array(x_test), np.array(y_train), np.array(y_test), \
           train_count, test_count, classes

def get_classes_map(y, y_raw):
    """
    :param y: binary representation of labels
    :param y_raw: list of languages in String form
    :return (OrderedDict): language to binary correspondence
    """
    classes = {}
    while len(classes) < len(Counter(y_raw)):
        for raw, category in zip(y_raw, y):
            classes[np.argmax(category)] = raw
    ordered_classes = OrderedDict(sorted(classes.items()))
    return ordered_classes

def save_input_data_to_files(x_train, x_test, y_train, y_test, train_count, test_count, classes):
    """
    Creates 2 files:
    - file with training and testing sets saved
    - file with information about classes distribution
    :param x_train: training feature matrices
    :param x_test: testing feature matrices
    :param y_train: corresponding training labels
    :param y_test: corresponding testing labels
    :param train_count: distribution by classes in training set
    :param test_count: distribution by classes in testing set
    :param classes: language to binary correspondence
    :return:
    """
    with open(info_data_npy, 'wb') as f:
        np.save(f, np.array([train_count, test_count]))
        np.save(f, classes)
    with open(features_npy, 'wb') as f:
        np.save(f, x_train)
        np.save(f, y_train)
        np.save(f, x_test)
        np.save(f, y_test)

def open_preprocessed_data():
    """
    Retrieves training and testing sets
    and information about classes distribution
    saved before from files.
    :return: (tuple) training samples, testing samples,
    training labels, testing labels,
    distribution by classes in training set,
    distribution by classes in testing set,
    language to binary correspondence
    """
    with open(features_npy, 'rb') as f:
        x_train = np.load(f)
        y_train = np.load(f)
        x_test = np.load(f)
        y_test = np.load(f)
    with open(info_data_npy, 'rb') as f:
        counts = np.load(f, allow_pickle=True)
        train_count = counts[0]
        test_count = counts[1]
        classes = np.load(f, allow_pickle=True).item()
    return x_train, x_test, y_train, y_test, train_count, test_count, classes

class TerminateOnBaseline(Callback):
    """
    Callback that terminates training when
    either accuracy or val_acc reaches
    a specified baseline
    """

    def __init__(self, monitor='accuracy', baseline=BASELINE):
        super(TerminateOnBaseline, self).__init__()
        self.monitor = monitor
        self.baseline = baseline

    def on_epoch_end(self, epoch, logs=None):
        logs = logs or {}
        accuracy = logs.get(self.monitor)
        if accuracy is not None:
            if accuracy >= self.baseline:
                logger.debug(f'Epoch {epoch}: Reached baseline, terminating training...')
                self.model.stop_training = True

class TimeHistory(Callback):
    """
    Callback that saves duration of every training epoch into list.
    """

    def on_train_begin(self, logs={}):
        self.times = []

    def on_epoch_begin(self, batch, logs={}):
        self.epoch_time_start = time.time()

    def on_epoch_end(self, batch, logs={}):
        self.times.append(time.time() - self.epoch_time_start)

def compare_sets(x_1, x_2):
    """
    :param x_1: (numpy.ndarray) list of 2-D numpy arrays; training set
    :param x_2: (numpy.ndarray) list of 2-D numpy arrays; testing set
    :return: String (how many occurrences have been found)
    """
    equal_matrices_num = 0
    indices_to_remove = []
    for matrix_idx, x_2_matrix in enumerate(x_2):
        for x_1_matrix in x_1:
            if (x_1_matrix == x_2_matrix).all():
                equal_matrices_num += 1
                indices_to_remove.append(matrix_idx)
                break
    return f'Number of equal matrices in sets: {equal_matrices_num}.'

def train_model(x_train, y_train, x_validation, y_validation):
    """
    Prepares data for training a 2D CNN model,
    builds a model,
    performs a model training,
    plots accuracy and loss changes during training.
    :param x_train: (numpy.ndarray) list of feature matrices for training the network
    :param y_train: (numpy.ndarray) list of binary labels training the network
    :param x_validation: (numpy.ndarray) list of feature matrices for testing the network
    :param y_validation: (numpy.ndarray) list of binary labels testing the network
    :return: Trained model
    """
    if CHECK_DATASETS:
        logger.debug('Checking whether train and test sets are different...')
        logger.debug(f'X train compared with itself. {compare_sets(x_train, x_train)}')
        logger.debug(f'X validation compared with itself. {compare_sets(x_validation, x_validation)}')
        logger.debug(f'X train compared with x validation. {compare_sets(x_train, x_validation)}')

    logger.debug('Getting data dimensions...')

    rows = x_train[0].shape[0]
    cols = x_train[0].shape[1]
    assert x_train[0].shape == x_validation[0].shape
    logger.debug('Train and validation matrices are of same dimension...')

    train_samples_num = x_train.shape[0]
    val_samples_num = x_validation.shape[0]
    assert train_samples_num == y_train.shape[0] and val_samples_num == y_validation.shape[0]
    logger.debug('X and Y have the same number of samples...')

    num_classes = y_train[0].shape[0]

    logger.debug(f'Input matrix rows: {rows}')
    logger.debug(f'Input matrix columns: {cols}')
    logger.debug(f'Num. of classes: {num_classes}')

    logger.debug('Reshaping input data...')

    input_shape = (rows, cols, 1)
    x_train = x_train.reshape(x_train.shape[0], rows, cols, 1)
    x_validation = x_validation.reshape(x_validation.shape[0], rows, cols, 1)
    logger.debug(f'Input data shape: {input_shape}')

    model = build_model(input_shape, num_classes)

    model.compile(loss='categorical_crossentropy',
                  optimizer='adam',
                  metrics=['accuracy'])

    logger.debug(f'Creating a condition for stopping training if accuracy does not change '
                 f'at least {MIN_DELTA * 100}% over {PATIENCE} epochs')

    es = EarlyStopping(monitor='accuracy', min_delta=MIN_DELTA, patience=PATIENCE, verbose=1, mode='auto',
                       restore_best_weights=True)
    # es_baseline = TerminateOnBaseline(monitor='accuracy', baseline=BASELINE)
    time_history = TimeHistory()

    logger.debug('Adding image generator for data augmentation...')
    data_generator = ImageDataGenerator(width_shift_range=0.2)

    logger.debug('Training model...')
    history = model.fit(data_generator.flow(x_train, y_train, batch_size=BATCH_SIZE),
                        steps_per_epoch=x_train.shape[0] / BATCH_SIZE, epochs=EPOCHS,
                        callbacks=[es, time_history], validation_data=(x_validation, y_validation))
    epoch_av_time = round(np.mean(time_history.times), 2)

    logger.debug('Model trained.')
    logger.info(f'Average epoch time: {epoch_av_time}')
    logger.debug('Plotting accuracy and loss...')

    plot_history(history)

    return model

def build_model(input_shape, num_classes):
    """
    Builds a 2D CNN model.
    :param input_shape: (tuple) shape of input data
    to pass to the 1st convolutional layer
    :param num_classes: (Int) number of classes for classification
    :return: Built Keras 2D CNN model
    """
    model = Sequential()
    # model.add(Conv2D(filters = 32, kernel_size = (5,5),padding = 'Same',activation ='relu', input_shape = (256,256,3)))
    model.add(Conv2D(32, kernel_size=KERNEL_SIZE, activation='relu',
                     data_format="channels_last",
                     input_shape=input_shape))
    model.add(BatchNormalization())
    # model.add(MaxPooling2D(pool_size=(2,2)))
    model.add(MaxPooling2D(pool_size=POOL_SIZE))
    # model.add(Conv2D(filters = 32, kernel_size = (5,5),padding = 'Same',activation ='relu', input_shape = (256,256,3)))
    model.add(Conv2D(64, kernel_size=KERNEL_SIZE, activation='relu'))
    model.add(BatchNormalization())
    # model.add(MaxPooling2D(pool_size=(2,2), strides=(2,2)))
    model.add(MaxPooling2D(pool_size=POOL_SIZE))


    if NUM_CNN_LAYERS == 4:
        model.add(Conv2D(filters = 96, kernel_size = (3,3),padding = 'Same',activation ='relu'))
        model.add(BatchNormalization())
        # if input shape height is less than 20
        # then only apply max pooling in x direction
        if input_shape[0] < 20:
            # apply horizontal pooling only
            print('Applying horizontal pooling only')
            model.add(MaxPooling2D(pool_size=(1, POOL_SIZE[1])))
        else:
            model.add(MaxPooling2D(pool_size=POOL_SIZE))
        model.add(Conv2D(filters = 96, kernel_size = (3,3),padding = 'Same',activation ='relu'))
        model.add(BatchNormalization())
        if input_shape[0] < 20:
            # apply horizontal pooling only
            print('Applying horizontal pooling only')
            model.add(MaxPooling2D(pool_size=(1, POOL_SIZE[1])))
        else:
            model.add(MaxPooling2D(pool_size=POOL_SIZE))
    elif NUM_CNN_LAYERS != 2:
        raise ValueError('NUM_CNN_LAYERS must be 2 or 4')
    
    model.add(Dropout(DROPOUT))
    # model.add(Flatten())
    model.add(Flatten())
    # model.add(Dense(512))
    # model.add(Activation('relu'))
    model.add(Dense(128, activation='relu'))
    model.add(Dropout(DROPOUT))
    # model.add(Dense(5, activation = "softmax"))
    model.add(Dense(num_classes, activation='softmax'))
    return model

def plot_history(history):
    """
    Plots how training and testing
    accuracy and loss change
    over the training process
    :param history: a model's training history
    :return:
    """
    fig, ax_loss = plt.subplots(constrained_layout=True)
    ax_acc = ax_loss.twinx()

    ax_loss.plot(history.history['loss'], label='train loss', color='#E43F04')
    ax_loss.plot(history.history['val_loss'], label='test loss', color='#FF9147')

    ax_acc.plot(history.history['accuracy'], label='train acc', color='#2201C7')
    ax_acc.plot(history.history['val_accuracy'], label='test acc', color='#0055FF')

    ax_loss.set_xlabel('epochs')
    ax_loss.set_ylabel('loss')
    ax_acc.set_ylabel('accuracy')

    ax_loss.legend(loc='upper left')
    ax_acc.legend(loc='lower left')

    plt.title('Model train vs validation')

    if USE_COMET_ML:
        experiment.log_figure(figure=plt)
    # plt.show()

def one_hot_to_int(one_hot_arr):
    """
    Convert one-hot encoded data to Int
    :param one_hot_arr: list of one-hot encoded numbers
    :return: list of numbers represented as integers
    """
    return np.array([np.argmax(one_hot) for one_hot in one_hot_arr])

def select_features(x_train, y_train, x_test):
    """
    Performs features selection by flattening
    feature matrices
    :param x_train: (numpy.ndarray) list of feature matrices used for training
    :param y_train: (numpy.ndarray) list of binary labels
    :param x_test: (numpy.ndarray) list of features matrices used for testing
    :return:
    """
    logger.debug('Performing feature selection...')
    logger.debug('[BEFORE SELECTION]')  # matrices won't pass for selection. Choose distinct vectors.
    logger.debug(f'X train shape: {x_train.shape}')
    logger.debug(f'y train shape: {y_train.shape}')
    logger.debug(f'X test shape: {x_test.shape}')

    y_train = one_hot_to_int(y_train)

    x_train = np.array([x_train.flatten() for x_train in x_train])
    x_test = np.array([x_test.flatten() for x_test in x_test])

    logger.debug('\n[AFTER SELECTION]')
    logger.debug(f'X train shape: {x_train.shape}')
    logger.debug(f'y train shape: {y_train.shape}')
    logger.debug(f'X test shape: {x_test.shape}')

    if selection_method == 'UNIVARIATE':
        selector = SelectKBest(score_func=SCORE_FUNC,
                               k=NUM_OF_FEATURES * FRAME_SIZE)  # k = number of features to choose
        selector.fit(x_train, y_train)
        logger.info(f'Feature selection score: [{selector.scores_}]')
    elif selection_method == 'PCE':
        selector = PCA(n_components=NUM_OF_FEATURES * FRAME_SIZE)
        selector.fit(x_train)
        logger.info(f'Explained Variance: {selector.explained_variance_ratio_}')
        logger.info(selector.components_)

    x_train_selected = selector.transform(x_train)
    x_test_selected = selector.transform(x_test)

    return x_train_selected, x_test_selected

def run(lang_set_config = LANG_SET,
        features_config = FEATURES, 
        num_seconds_config = NUM_SECONDS, 
        expt_name_config = EXPT_NAME,
        project_name_config = COMET_PROJECT_NAME,
        filter_input_data_config = FILTER_INPUT_DATA,
        audio_input_path_config = AUDIO_INPUT_PATH,
        cnn_layers_config = NUM_CNN_LAYERS,
        max_pool_4_config = MAX_POOL_4
        ):
    global LANG_SET
    global FEATURES
    global NUM_SECONDS
    global EXPT_NAME
    global COMET_PROJECT_NAME
    global FILTER_INPUT_DATA
    global AUDIO_INPUT_PATH
    global NUM_CNN_LAYERS
    global MAX_POOL_4

    COMET_PROJECT_NAME = project_name_config
    EXPT_NAME = expt_name_config
    LANG_SET = lang_set_config
    FEATURES = features_config
    NUM_SECONDS = num_seconds_config
    FILTER_INPUT_DATA = filter_input_data_config
    AUDIO_INPUT_PATH = audio_input_path_config
    NUM_CNN_LAYERS = cnn_layers_config
    MAX_POOL_4 = max_pool_4_config

    performance_metrics = main()
    return performance_metrics

def log_classification_report(y_test_bool, y_predicted, target_names):
    report = classification_report(y_test_bool, y_predicted, target_names=target_names, output_dict=True)
    for key, value in report.items():
      if key == "accuracy":
        experiment.log_metric( "final_"+key, value)
      else:
        experiment.log_metrics(value, prefix=f'{key}')

def main():
    """
    Script performing data preparation,
    model building and training as well as
    model evaluation.
    :return:
    """
    global LANG_SET
    global features_npy, info_data_npy

    logger.debug('Setting up file paths according to the set up...')

    if UNSILENCE:
        LANG_SET = LANG_SET + '_unsilenced'
    training_languages_str = f'{MAX_PER_LANG}_{LANG_SET}_{FRAME_SIZE}'

    create_experiment()

    logger.debug('Creating saving directories if they do not yet exist..')
    # get the last element of the path
    audio_input_path_split = AUDIO_INPUT_PATH.split('/')[-1]
    Path(f'./features/{FEATURES}/{audio_input_path_split}').mkdir(parents=True, exist_ok=True)
    Path(f'./testing_data/{FEATURES}/{audio_input_path_split}').mkdir(parents=True, exist_ok=True)
    Path(f'./models/{FEATURES}/{audio_input_path_split}').mkdir(parents=True, exist_ok=True)

    logger.debug('Defining saving file names...')

    features_npy = f'./features/{FEATURES}/{audio_input_path_split}/{training_languages_str}.npy'
    info_data_npy = f'./testing_data/{FEATURES}/{audio_input_path_split}/{training_languages_str}.npy'
    model_file = f'./models/{FEATURES}/{audio_input_path_split}/{training_languages_str}.h5'


    if OVERWRITE_FEATURE_FILES or not Path.exists(Path(features_npy)) or not Path.exists(Path(info_data_npy)):
        logger.debug('Preprocessing audios...')
        dir = AUDIO_INPUT_PATH
        csv_path = create_file_data_csv(dir)
        df = pd.read_csv(csv_path)

        df = filter_df(df)  
        
        audio_paths = df.filepath
        corresponding_languages = df.language

        preprocess = preprocess_new_data(audio_paths, corresponding_languages)
        if not preprocess:
            return -1
        x_train, x_test, y_train, y_test, train_count, test_count, languages_mapping = preprocess
    else:
        logger.debug('Getting input data from file: ' + features_npy)
        x_train, x_test, y_train, y_test, train_count, test_count, languages_mapping = open_preprocessed_data()

    logger.debug('Selecting features...')

    if SELECT_FEATURES:
        x_train, x_test = select_features(x_train, y_train, x_test)
        x_train, x_test = create_segments_after_selection((x_train, x_test))

    if OVERWRITE_MODEL_FILES or not Path.exists(Path(model_file)):
        logger.debug('Training model...')
        trained_model = train_model(np.array(x_train), np.array(y_train), np.array(x_test), np.array(y_test))
        trained_model.summary()
        trained_model.save(model_file)
    else:
        logger.debug('Found trained model. Loading...')
        trained_model = load_model(model_file)

    languages_classes_mapping = list(languages_mapping.values())

    logger.debug('Running model on testing set...')
    logger.debug(f'X train shape: {x_train.shape}')
    logger.debug(f'X test shape: {x_test.shape}')
    logger.debug(f'Y train shape: {y_train.shape}')
    logger.debug(f'Y test shape: {y_test.shape}')

    y_predicted = np.argmax(trained_model.predict(x_test.reshape(x_test.shape + (1,)), verbose=1), axis=1)
    y_test_bool = np.argmax(y_test, axis=1)
    report = classification_report(y_test_bool, y_predicted, target_names=languages_classes_mapping, output_dict=True)
    logger.info(f'Metrics:\n{report}')
    log_classification_report(y_test_bool, y_predicted, target_names=languages_classes_mapping)
    logger.debug('Printing statistics (training ans testing counters)...')
    logger.info(f'Training samples: {train_count}')
    logger.info(f'Testing samples: {test_count}')

    if USE_COMET_ML:
        logger.debug('Displaying a confusion matrix, overall accuracy...')
        cm = ConfusionMatrix()
        cm.compute_matrix(y_test, y_predicted)
        cm.labels = languages_classes_mapping
        confusion_matrix = np.array(cm.to_json()['matrix'])

        experiment.log_confusion_matrix(matrix=cm)

        # log_classification_report(y_train, trained_model.predict(x_train.reshape(x_test.shape + (1,)), verbose=1))

        logger.debug('Accuracy to beat = (samples of most common class) / (all samples)')
        acc_to_beat = np.amax(np.sum(confusion_matrix, axis=1) / np.sum(confusion_matrix))
        confusion_matrix_acc = np.sum(confusion_matrix.diagonal()) / float(np.sum(confusion_matrix))
        trained_model.evaluate(x_test.reshape(x_test.shape + (1,)), y_test)

        logger.info(f'Accuracy to beat: {acc_to_beat}')
        logger.info(f'Confusion matrix:\n {confusion_matrix}')
        logger.info(f'Accuracy: {confusion_matrix_acc}')
        logger.debug('Displaying the baseline, and whether it has been hit...')

        baseline_difference = confusion_matrix_acc - acc_to_beat
        if baseline_difference < 0:
            logger.info('Baseline has not been hit.')
        else:
            logger.info(f'Baseline score: {baseline_difference}')
    else:  # no Comet ML
        trained_model.evaluate(x_test.reshape(x_test.shape + (1,)), y_test, verbose=1)
        logger.info(f'Comet ML API_KEY and other variables are not found')
        logger.info(f'Confusion Matrix accuracy calculations are not performed')

    logger.debug('Showing languages to categorical mapping...')
    logger.info(f'Relation classes to categories: {languages_mapping}')

    y_predicted_prob = trained_model.predict(x_test.reshape(x_test.shape + (1,)), verbose=1)
    logger.info(y_predicted[:10])
    logger.info('PROB: ')
    logger.info(y_predicted_prob[:10])

    performance_metrics = {
        'accuracy': confusion_matrix_acc
        # 'precision': 
        # 'recall': recall_score(y_test, y_pred),
        # 'f1_score': f1_score(y_test, y_pred)
    }
    return performance_metrics


if __name__ == '__main__':
    main()

