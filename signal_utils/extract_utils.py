﻿import collections
from os import path

import numpy as np
from tqdm import tqdm
from scipy.optimize import curve_fit
from scipy.signal import argrelextrema

from gdrive_utils import load_dataset
from generation_utils import gen_multiple, gen_signal


def extract_fit_all(data, start_time, threshold, sample_freq,
                    pos_step=2.0, amp_step=500.0):
    """
      Выделение событий с помощью фитирования всех событий одновременно. 
      Классификация наложений производится полносвязной нейронной сетью.
      
      Алгоритм требует высокой производительности, но дает наиболее точный 
      результат.
      
      Алгоритм:
          1. Выделение локальных максимумов в кадре выше порога. Локальные 
          максимумы считаются предварительными пиками.
          2. Классификация наложенных событий с помощью НС по предварительным
          пикам.
          3. Одновременное фитирование всех пиков. В качестве начальных данных
          берутся предварительные пики и соответсвующие им амплитуды. Диапазоны
          фитирования задаются значениями pos_step и amp_step.
          
    """
    if not extract_fit_all.model:
        if not 'load_model' in locals():
            from keras.models import load_model
        extract_fit_all.model = load_model(path.join(path.dirname(__file__), 
                                           "data/mlp_classifier.h5"))
    
    peaks = get_peaks(data, threshold)
    
    if not len(peaks):
        return np.array([]), np.array([])
    
    prep_peaks = np.vstack([data[peak - extract_fit_all.l_off: \
                                 peak + extract_fit_all.r_off]\
                           for peak in peaks])
    singles = extract_fit_all.model.predict(prep_peaks/extract_fit_all.x_max)\
                           .argmax(axis=1).astype(np.bool)
                           
    params = extract_events_fit_all(data, threshold, amp_step, pos_step)
    
    params[1::2] = ((params[1::2]/sample_freq)*1e+9) + start_time
    
    return params, singles

extract_fit_all.model = None
extract_fit_all.frame_len = 50
extract_fit_all.l_off = 14   
extract_fit_all.r_off = extract_fit_all.frame_len - extract_fit_all.l_off
extract_fit_all.x_min = -32768
extract_fit_all.x_max = 32768


def extract_simple_amps(data, start_time, threshold, sample_freq):
    """
      Выделение событий поиском локальных максимумов выше порога.
      
      Алгоритм является наиболее быстрым, однако он не учитывает форму сигнала,
      что приводит к систематической ошибке выделения амлитуд близких событий
      (событие попадает на хвост другого события).
      
    """
    
    peaks = get_peaks(data, threshold)
    
    params = np.zeros(len(peaks)*2, np.float64)
    params[0::2] = data[peaks]
    params[1::2] = ((peaks/sample_freq)*1e+9) + start_time
    singles = np.ones(peaks.shape, np.bool)
    
    return params, singles


def extract_amps_approx(data, start_time, threshold, sample_freq):
    """
      Последовательное выделение событий из блока с вычитанием предыдущих 
      событий.
      
      Алгоритм имеет меньшую скорость работы по сравнению с 
      extract_simple_amps, однако учитывет форму сигнала.
      
      Алгоритм:
          1. Выделение локальных максимумов в кадре выше порога.
          2. Последовательная обработка пиков:
              1. Сохранение амплитуды и положения текущего пика.
              2. Вычитание из данных формы сигнала, соответсвующей выделенным
              амплитуде и положению текущего события.
              3. Переход к следующему событию.
      
    """
    data = data.copy()
    peaks = get_peaks(data, threshold)
    
    params = np.zeros(len(peaks)*2, np.float32)
    params[1::2] = ((peaks/sample_freq)*1e+9) + start_time

    x = np.arange(len(data))
    for i in range(len(peaks)):
        peak = peaks[i]
        amp = data[peak]
        params[i*2] = amp     
        data -= gen_multiple(x, amp, peak) 
    
    singles = np.ones(peaks.shape, np.bool)
    
    return params, singles


def extract_amps_approx2(data, start_time, threshold, sample_freq,
                         classify=False, frame_l=15, frame_r=25):
    """
      Последовательное выделение событий из блока с вычитанием предыдущих 
      событий.
      
      Ускоренный вариант extract_amps_approx. Вместо вычета события из всего 
      кадра, событие вычитается только из пиков. 
      
      Примерно на 30% медленее extract_simple_amps.
      
      @todo: Разобраться с сохранением времени в наносекундах
    """
    
    peaks = get_peaks(data, threshold)
    if classify:
        if not extract_amps_approx2.bst:
            if not 'load_model' in locals():
                global xgb
                xgb = __import__('xgboost')
            extract_amps_approx2.bst = xgb.Booster()
            model_path = path.join(path.dirname(__file__), 'data/xgb.dat') 
            extract_amps_approx2.bst.load_model(model_path)

        frames = extract_frames(data.copy(), peaks, frame_l, frame_r)
        preds = extract_amps_approx2.bst.predict(xgb.DMatrix(frames))
        singles = preds > 0.97
    else:
        singles = np.ones(peaks.shape, np.bool)
    
    params = np.zeros(len(peaks)*2, np.float32)
    params[0::2] = data[peaks]
    params[1::2] = ((peaks/sample_freq)*1e+9) + start_time
    
    for i, peak in enumerate(peaks[:-1]):
        params[(i + 1)*2::2] -= gen_signal(peaks[i + 1:], params[i*2], peak)
    
    return params, singles
extract_amps_approx2.bst = None


def extract_frames(ev_data, peaks, frame_l, frame_r):
    """Выделение событий из кадра по пикам.
    @ev_data - массив кадра
    @peaks - массив положений пиков в кадре в бинах
    @frame_l - размер обрезания события слева от пика
    @frame_r - размер обрезания события справа от пика
    @return - Двумерный массив событий.
    """
    shape = (len(peaks), frame_l + frame_r)
    frames_block = np.zeros(shape, np.int16)
    for j, peak in enumerate(peaks):
        if peak >= frame_l and peak < len(ev_data) - frame_r:
            frames_block[j] = ev_data[int(peak) - frame_l: 
                                      int(peak) + frame_r]
    return frames_block


def calc_center(ev, amp_arg, step=2):
    """
      Параболическая аппроксимация центра пика
      
      @ev - массив события
      @amp_arg - положение пика
      
    """
    x1 = amp_arg
    y1 = ev[x1]
    x2 = x1 + step
    y2 = ev[x2]
    x3 = x1 - step
    y3 = ev[x3]

    a = (y3 - (x3*(y2-y1) + x2*y1 - x1*y2)/(x2 - x1))/\
    (x3*(x3 - x1 - x2) + x1*x2)
    b = (y2 - y1)/(x2 - x1) - a*(x1 + x2)
    c = (x2*y1 - x1*y2)/(x2 - x1) + a*x1*x2
    
    x0 = -b/(2*a)
    y0 = a*x0**2 + b*x0 + c
    
    return y0, x0 


def get_peaks(ev, threshold):
    """
      Выделение пиков из блока
      Условия отбора пика:
      - пик должен быть больше порогового значения
      - пик должен быть локальным экстремумом
      
      @ev - массив события
      @threshold - порог
      
    """
    extremas = argrelextrema(ev, np.greater_equal)[0]
    points = extremas[ev[extremas] >= threshold]
    
    if len(points) >= 2:
        planes = np.where(points[1:] - points[:-1] <= 3)[0] + 1
        points = np.delete(points, planes)
    
    return points


def extract_events_fit(ev, threshold):
    """
      Последовательное выделение событий из блока
      
      В блоке последовательно фитируются событие. Функция следующего события 
      складывается с фунциями уже определенных ранее событий.
      
      Данный метод работает потенциально быстрее и стабильнее по сравнению с 
      одновременным фитированием всех событий в блоке, выдает большую ошибку 
      при выделении близко наложенных событий (событие накладывается на хвост 
      предыдущего события).
      
      @ev - массив события
      @threshold - порог
      @return параметры событий в формате [amp1, pos1, amp2, pos2, ...]
      
    """
    points = get_peaks(ev, threshold)
    values = np.array([], np.float32)

    for i, point in enumerate(points):
        y0, x0 = calc_center(ev, point)

        full_func = lambda x, a, p: gen_multiple(x, a, p, *values)

        popt, pcov = curve_fit(full_func, np.arange(len(ev)), ev, p0=[y0, x0])
        values = np.hstack([values, popt])

    return values


def extract_events_fit_all(ev, threshold, pos_step=2.0, amp_step=500.0):
    """
      Последовательное выделение событий из блока
      Все события фитируются одновременно.
      
      @ev - массив события
      @threshold - порог
      @pos_step - максимальное отклонение по положению при фитировании
      @amp_step - максимальное положительное отклонение по амплитуде при 
      фитировании. Нижняя граница всегда равна 0.
      @return параметры событий в формате [amp1, pos1, amp2, pos2, ...]
      
    """
    points = get_peaks(ev, threshold)   
    
    values = np.zeros(len(points)*2)
    values[0::2] = ev[points]
    values[1::2] = points
    
    upper_bounds = values.copy()
    upper_bounds[1::2] = upper_bounds[1::2] + pos_step
    upper_bounds[0::2] = upper_bounds[0::2] + amp_step
                
    lower_bounds = values.copy()
    lower_bounds[1::2] = lower_bounds[1::2] - pos_step
    lower_bounds[0::2] = 0
    
    full_func = lambda x, *values: gen_multiple(x, *values)
    popt, pcov = curve_fit(full_func, np.arange(len(ev)), ev, p0=list(values))

    return popt


def extract_from_dataset(dataset, threshold=700, 
                         area_l=50, area_r=100):
    """
      Получение блоков из датасета [Desperated]
      @dataset - датасет
      @threshold - порог zero-suppression
      @area_l - левая область zero-suppression
      @area_r - правая область zero-suppression
      @return - вырезанные блоки
      
    """
    frames = [] # кадры из точки
    for i in range(dataset.params["events_num"]):
        try:
            frames.append(dataset.get_event(i)["data"])
        except:
            break

    blocks = [] # вырезанные из кадра события
    for frame in frames:
        peaks = np.where(frame > threshold)[0]
        dists = peaks[1:] - peaks[:-1]
        gaps = np.append(np.array([0]), np.where(dists > area_r)[0] + 1)
        for gap in range(0, len(gaps) - 1):
            l_bord = peaks[gaps[gap]] - area_l
            r_bord = peaks[gaps[gap + 1] - 1] + area_r
            blocks.append(frame[l_bord: r_bord])
    return blocks


def get_blocks(points, idxs, threshold=700, area_l=50, area_r=100):
    """
      [Desperated]
      Выделение из файлов google drive блоков алгоритмом zero-suppression
      @points - таблица с точками
      @idxs - индекс или массив индексов интересующих точек
      @threshold - порог zero-suppression
      @area_l - левая область zero-suppression
      @area_r - правая область zero-suppression
      @return - вырезанные блоки
      
    """
    blocks = []
    
    def add_blocks(blocks, idx):
        header = points.as_matrix()[idx]
        dataset = load_dataset(header[0], header[1], header[2])
        blocks += extract_from_dataset(dataset, threshold, area_l, area_r)
    
    if isinstance(idxs, collections.Iterable):
        for i in tqdm(idxs):
            add_blocks(blocks, i)
    else:
        add_blocks(blocks, idxs)
    
    return blocks


def get_bin_sec(points, idx):
    """
      [Desperated]
      Получение из файла google drive частоты оцифровки
      @points - таблица с точками
      @idx - индекс точки, из которой берется частота оцифровки
      
    """
    header = points.as_matrix()[idx]
    dataset = load_dataset(header[0], header[1], header[2])
    return dataset.params["sample_freq"]**-1


def extract_algo(data, threshold=700):
    """
      Выделенние события из блока. Способ, предложенный Пантуевым В.С.
      @data - массив кадра
      @threshold - порог
      @return - индекс первого бина, превысившего порог,
      индекс перегиба, индекс первого отрицательного бина после перегиба
      
    """
    deriv = data[1:] - data[:-1]
    first_greater = np.argmax(data >= threshold)
    extremum = first_greater + np.argmax(deriv[first_greater-1:] < 0) - 1
    first_negative = first_greater + np.argmax(data[first_greater:] < 0)
    return first_greater, extremum, first_negative


def extract_events(data, threshold=700):
    """
      Выделенние нескольких событий из блока. 
      Способ, предложенный Пантуевым В.С.
      @data - массив кадра
      @threshold - порог
      @return - [индекс первого бина, превысившего порог,
      индекс перегиба, индекс первого отрицательного бина после перегиба]
      
    """
    events = []
    offset = 0
    while(True):
        left, center, right = extract_algo(data[offset:], threshold)
        if not left:
            break
        left += offset
        center += offset
        r_buf = right
        right += offset
        offset += r_buf
        events.append([left, center, right])
        
    return events
