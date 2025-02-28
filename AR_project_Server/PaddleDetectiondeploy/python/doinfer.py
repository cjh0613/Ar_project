import os,sys
import time
from functools import reduce
from multiprocessing import Process, Queue

import cv2
import numpy
import numpy as np
import paddle.fluid as fluid
import yaml
from PIL import Image


from visualize import visualize_box_mask


def decode_image(im_file, im_info):
    """read rgb image
    Args:
        im_file (str/np.ndarray): path of image/ np.ndarray read by cv2
        im_info (dict): info of image
    Returns:
        im (np.ndarray):  processed image (np.ndarray)
        im_info (dict): info of processed image
    """
    if isinstance(im_file, str):
        with open(im_file, 'rb') as f:
            im_read = f.read()
        data = np.frombuffer(im_read, dtype='uint8')
        im = cv2.imdecode(data, 1)  # BGR mode, but need RGB mode
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        im_info['origin_shape'] = im.shape[:2]
        im_info['resize_shape'] = im.shape[:2]
    else:
        im = im_file
        im_info['origin_shape'] = im.shape[:2]
        im_info['resize_shape'] = im.shape[:2]
    return im, im_info


class Resize(object):
    """resize image by target_size and max_size
    Args:
        arch (str): model type
        target_size (int): the target size of image
        max_size (int): the max size of image
        use_cv2 (bool): whether us cv2
        image_shape (list): input shape of model
        interp (int): method of resize
    """

    def __init__(self,
                 arch,
                 target_size,
                 max_size,
                 use_cv2=True,
                 image_shape=None,
                 interp=cv2.INTER_LINEAR):
        self.target_size = target_size
        self.max_size = max_size
        self.image_shape = image_shape,
        self.arch = arch
        self.use_cv2 = use_cv2
        self.interp = interp
        self.scale_set = {'RCNN', 'RetinaNet'}

    def __call__(self, im, im_info):
        """
        Args:
            im (np.ndarray): image (np.ndarray)
            im_info (dict): info of image
        Returns:
            im (np.ndarray):  processed image (np.ndarray)
            im_info (dict): info of processed image
        """
        im_channel = im.shape[2]
        im_scale_x, im_scale_y = self.generate_scale(im)
        if self.use_cv2:
            im = cv2.resize(
                im,
                None,
                None,
                fx=im_scale_x,
                fy=im_scale_y,
                interpolation=self.interp)
        else:
            resize_w = int(im_scale_x * float(im.shape[1]))
            resize_h = int(im_scale_y * float(im.shape[0]))
            if self.max_size != 0:
                raise TypeError(
                    'If you set max_size to cap the maximum size of image,'
                    'please set use_cv2 to True to resize the image.')
            im = im.astype('uint8')
            im = Image.fromarray(im)
            im = im.resize((int(resize_w), int(resize_h)), self.interp)
            im = np.array(im)

        # padding im when image_shape fixed by infer_cfg.yml
        if self.max_size != 0 and self.image_shape is not None:
            padding_im = np.zeros(
                (self.max_size, self.max_size, im_channel), dtype=np.float32)
            im_h, im_w = im.shape[:2]
            padding_im[:im_h, :im_w, :] = im
            im = padding_im

        if self.arch in self.scale_set:
            im_info['scale'] = im_scale_x
        im_info['resize_shape'] = im.shape[:2]
        return im, im_info

    def generate_scale(self, im):
        """
        Args:
            im (np.ndarray): image (np.ndarray)
        Returns:
            im_scale_x: the resize ratio of X 
            im_scale_y: the resize ratio of Y 
        """
        origin_shape = im.shape[:2]
        im_c = im.shape[2]
        if self.max_size != 0 and self.arch in self.scale_set:
            im_size_min = np.min(origin_shape[0:2])
            im_size_max = np.max(origin_shape[0:2])
            im_scale = float(self.target_size) / float(im_size_min)
            if np.round(im_scale * im_size_max) > self.max_size:
                im_scale = float(self.max_size) / float(im_size_max)
            im_scale_x = im_scale
            im_scale_y = im_scale
        else:
            im_scale_x = float(self.target_size) / float(origin_shape[1])
            im_scale_y = float(self.target_size) / float(origin_shape[0])
        return im_scale_x, im_scale_y


class Normalize(object):
    """normalize image
    Args:
        mean (list): im - mean
        std (list): im / std
        is_scale (bool): whether need im / 255
        is_channel_first (bool): if True: image shape is CHW, else: HWC
    """

    def __init__(self, mean, std, is_scale=True, is_channel_first=False):
        self.mean = mean
        self.std = std
        self.is_scale = is_scale
        self.is_channel_first = is_channel_first

    def __call__(self, im, im_info):
        """
        Args:
            im (np.ndarray): image (np.ndarray)
            im_info (dict): info of image
        Returns:
            im (np.ndarray):  processed image (np.ndarray)
            im_info (dict): info of processed image
        """
        im = im.astype(np.float32, copy=False)
        if self.is_channel_first:
            mean = np.array(self.mean)[:, np.newaxis, np.newaxis]
            std = np.array(self.std)[:, np.newaxis, np.newaxis]
        else:
            mean = np.array(self.mean)[np.newaxis, np.newaxis, :]
            std = np.array(self.std)[np.newaxis, np.newaxis, :]
        if self.is_scale:
            im = im / 255.0
        im -= mean
        im /= std
        return im, im_info


class Permute(object):
    """permute image
    Args:
        to_bgr (bool): whether convert RGB to BGR 
        channel_first (bool): whether convert HWC to CHW
    """

    def __init__(self, to_bgr=False, channel_first=True):
        self.to_bgr = to_bgr
        self.channel_first = channel_first

    def __call__(self, im, im_info):
        """
        Args:
            im (np.ndarray): image (np.ndarray)
            im_info (dict): info of image
        Returns:
            im (np.ndarray):  processed image (np.ndarray)
            im_info (dict): info of processed image
        """
        if self.channel_first:
            im = im.transpose((2, 0, 1)).copy()
        if self.to_bgr:
            im = im[[2, 1, 0], :, :]
        return im, im_info


class PadStride(object):
    """ padding image for model with FPN 
    Args:
        stride (bool): model with FPN need image shape % stride == 0 
    """

    def __init__(self, stride=0):
        self.coarsest_stride = stride

    def __call__(self, im, im_info):
        """
        Args:
            im (np.ndarray): image (np.ndarray)
            im_info (dict): info of image
        Returns:
            im (np.ndarray):  processed image (np.ndarray)
            im_info (dict): info of processed image
        """
        coarsest_stride = self.coarsest_stride
        if coarsest_stride == 0:
            return im
        im_c, im_h, im_w = im.shape
        pad_h = int(np.ceil(float(im_h) / coarsest_stride) * coarsest_stride)
        pad_w = int(np.ceil(float(im_w) / coarsest_stride) * coarsest_stride)
        padding_im = np.zeros((im_c, pad_h, pad_w), dtype=np.float32)
        padding_im[:, :im_h, :im_w] = im
        im_info['resize_shape'] = padding_im.shape[1:]
        return padding_im, im_info


def create_inputs(im, im_info, model_arch='YOLO'):
    """generate input for different model type
    Args:
        im (np.ndarray): image (np.ndarray)
        im_info (dict): info of image
        model_arch (str): model type
    Returns:
        inputs (dict): input of model
    """
    inputs = {}
    inputs['image'] = im
    origin_shape = list(im_info['origin_shape'])
    resize_shape = list(im_info['resize_shape'])
    scale = im_info['scale']
    if 'YOLO' in model_arch:
        im_size = np.array([origin_shape]).astype('int32')
        inputs['im_size'] = im_size
    elif 'RetinaNet' in model_arch:
        im_info = np.array([resize_shape + [scale]]).astype('float32')
        inputs['im_info'] = im_info
    elif 'RCNN' in model_arch:
        im_info = np.array([resize_shape + [scale]]).astype('float32')
        im_shape = np.array([origin_shape + [1.]]).astype('float32')
        inputs['im_info'] = im_info
        inputs['im_shape'] = im_shape
    return inputs


class Config():
    """set config of preprocess, postprocess and visualize
    Args:
        model_dir (str): root path of model.yml
    """
    support_models = ['YOLO', 'SSD', 'RetinaNet', 'RCNN', 'Face']

    def __init__(self, model_dir):
        # parsing Yaml config for Preprocess
        deploy_file = os.path.join(model_dir, 'infer_cfg.yml')
        with open(deploy_file) as f:
            yml_conf = yaml.safe_load(f)
        self.check_model(yml_conf)
        self.arch = yml_conf['arch']
        self.preprocess_infos = yml_conf['Preprocess']
        self.use_python_inference = yml_conf['use_python_inference']
        self.min_subgraph_size = yml_conf['min_subgraph_size']
        self.labels = yml_conf['label_list']
        self.mask_resolution = None
        if 'mask_resolution' in yml_conf:
            self.mask_resolution = yml_conf['mask_resolution']
        self.print_config()

    def check_model(self, yml_conf):
        """
        Raises:
            ValueError: loaded model not in supported model type 
        """
        for support_model in self.support_models:
            if support_model in yml_conf['arch']:
                return True
        raise ValueError(
            "Unsupported arch: {}, expect SSD, YOLO, RetinaNet, RCNN and Face".
                format(yml_conf['arch']))

    def print_config(self):
        print('-----------  Model Configuration -----------')
        print('%s: %s' % ('Model Arch', self.arch))
        print('%s: %s' % ('Use Padddle Executor', self.use_python_inference))
        print('%s: ' % ('Transform Order'))
        for op_info in self.preprocess_infos:
            print('--%s: %s' % ('transform op', op_info['type']))
        print('--------------------------------------------')


def load_predictor(model_dir,
                   run_mode='fluid',
                   batch_size=1,
                   use_gpu=False,
                   min_subgraph_size=3):
    """set AnalysisConfig, generate AnalysisPredictor
    Args:
        model_dir (str): root path of __model__ and __params__
        use_gpu (bool): whether use gpu
    Returns:
        predictor (PaddlePredictor): AnalysisPredictor
    Raises:
        ValueError: predict by TensorRT need use_gpu == True.
    """
    if not use_gpu and not run_mode == 'fluid':
        raise ValueError(
            "Predict by TensorRT mode: {}, expect use_gpu==True, but use_gpu == {}"
                .format(run_mode, use_gpu))
    if run_mode == 'trt_int8':
        raise ValueError("TensorRT int8 mode is not supported now, "
                         "please use trt_fp32 or trt_fp16 instead.")
    precision_map = {
        'trt_int8': fluid.core.AnalysisConfig.Precision.Int8,
        'trt_fp32': fluid.core.AnalysisConfig.Precision.Float32,
        'trt_fp16': fluid.core.AnalysisConfig.Precision.Half
    }
    config = fluid.core.AnalysisConfig(
        os.path.join(model_dir, '__model__'),
        os.path.join(model_dir, '__params__'))
    if use_gpu:
        # initial GPU memory(M), device ID
        config.enable_use_gpu(500, 0)
        # optimize graph and fuse op
        config.switch_ir_optim(True)
    else:
        config.disable_gpu()

    if run_mode in precision_map.keys():
        config.enable_tensorrt_engine(
            workspace_size=1 << 10,
            max_batch_size=batch_size,
            min_subgraph_size=min_subgraph_size,
            precision_mode=precision_map[run_mode],
            use_static=False,
            use_calib_mode=False)

    # disable print log when predict
    config.disable_glog_info()
    # enable shared memory
    config.enable_memory_optim()
    # disable feed, fetch OP, needed by zero_copy_run
    config.switch_use_feed_fetch_ops(False)
    predictor = fluid.core.create_paddle_predictor(config)
    return predictor


def load_executor(model_dir, use_gpu=False):
    if use_gpu:
        place = fluid.CUDAPlace(0)
    else:
        place = fluid.CPUPlace()
    exe = fluid.Executor(place)
    program, feed_names, fetch_targets = fluid.io.load_inference_model(
        dirname=model_dir,
        executor=exe,
        model_filename='__model__',
        params_filename='__params__')
    return exe, program, fetch_targets


def print_arguments(args):
    print('-----------  Running Arguments -----------')
    for arg, value in sorted(vars(args).items()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------')


def pi_visualize(image_file,
                 results,
                 labels,
                 mask_resolution=14,
                  video_writer=None):
    # visualize the predict result
    im = visualize_box_mask(
        image_file, results, labels, mask_resolution=mask_resolution)
    # img_name = os.path.split(image_file)[-1]
    # if not os.path.exists(output_dir):
    #     os.makedirs(output_dir)
    # out_path = os.path.join(output_dir, img_name)
    # img = cv2.cvtColor(, cv2.COLOR_RGB2BGR)
    img = numpy.asarray(im)

    cv2.imshow("mask", img)
    if video_writer is not None:
        video_writer.write(img)
    c = cv2.waitKey(1)
    # if c & 0xFF == ord('q'):
    #     video_writer.release()
    # print("标注用时:", time.time() - t1)


def visualize(image_file,
              results,
              labels,
              mask_resolution=14,
              output_dir='output/'):
    # visualize the predict result
    im = visualize_box_mask(
        image_file, results, labels, mask_resolution=mask_resolution)
    img_name = os.path.split(image_file)[-1]
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    out_path = os.path.join(output_dir, img_name)
    im.save(out_path, quality=95)
    print("save result to: " + out_path)


class Detector:
    """
    Args:
        model_dir (str): root path of __model__, __params__ and infer_cfg.yml
        use_gpu (bool): whether use gpu
    """

    def __init__(self,
                 model_dir,
                 use_gpu=True,
                 run_mode='fluid',
                 threshold=0.5):
        self.config = Config(model_dir)
        if self.config.use_python_inference:
            self.executor, self.program, self.fecth_targets = load_executor(
                model_dir, use_gpu=use_gpu)
        else:
            self.predictor = load_predictor(
                model_dir,
                run_mode=run_mode,
                min_subgraph_size=self.config.min_subgraph_size,
                use_gpu=use_gpu)
        self.preprocess_ops = []
        for op_info in self.config.preprocess_infos:
            op_type = op_info.pop('type')
            if op_type == 'Resize':
                op_info['arch'] = self.config.arch
            self.preprocess_ops.append(eval(op_type)(**op_info))

    def preprocess(self, im):
        # process image by preprocess_ops
        im_info = {
            'scale': 1.,
            'origin_shape': None,
            'resize_shape': None,
        }
        im, im_info = decode_image(im, im_info)
        for operator in self.preprocess_ops:
            im, im_info = operator(im, im_info)
        im = np.array((im,)).astype('float32')
        inputs = create_inputs(im, im_info, self.config.arch)
        return inputs, im_info

    def postprocess(self, np_boxes, np_masks, im_info, threshold=0.5):
        # postprocess output of predictor
        results = {}
        if self.config.arch in ['SSD', 'Face']:
            w, h = im_info['origin_shape']
            np_boxes[:, 2] *= h
            np_boxes[:, 3] *= w
            np_boxes[:, 4] *= h
            np_boxes[:, 5] *= w
        expect_boxes = np_boxes[:, 1] > threshold
        np_boxes = np_boxes[expect_boxes, :]
        # for box in np_boxes:
        #     print('class_id:{:d}, confidence:{:.2f},'
        #           'left_top:[{:.2f},{:.2f}],'
        #           ' right_bottom:[{:.2f},{:.2f}]'.format(
        #         int(box[0]), box[1], box[2], box[3], box[4], box[5]))
        results['boxes'] = np_boxes
        if np_masks is not None:
            np_masks = np_masks[expect_boxes, :, :, :]
            results['masks'] = np_masks
        return results

    def predict(self, image, threshold=0.5, warmup=0, repeats=1):
        """
        Args:
            image (str/np.ndarray): path of image/ np.ndarray read by cv2
            threshold (float): threshold of predicted box' score
        Returns:
            results (dict): include 'boxes': np.ndarray: shape:[N,6], N: number of box,
                            matix element:[class, score, x_min, y_min, x_max, y_max]
                            MaskRCNN's results include 'masks': np.ndarray:
                            shape:[N, class_num, mask_resolution, mask_resolution]
        """
        inputs, im_info = self.preprocess(image)
        np_boxes, np_masks = None, None
        if self.config.use_python_inference:
            for i in range(warmup):
                outs = self.executor.run(self.program,
                                         feed=inputs,
                                         fetch_list=self.fecth_targets,
                                         return_numpy=False)
            t1 = time.time()
            for i in range(repeats):
                outs = self.executor.run(self.program,
                                         feed=inputs,
                                         fetch_list=self.fecth_targets,
                                         return_numpy=False)
            t2 = time.time()
            ms = (t2 - t1) * 1000.0 / repeats
            # print("Inference: {} ms per batch image".format(ms))

            np_boxes = np.array(outs[0])
            if self.config.mask_resolution is not None:
                np_masks = np.array(outs[1])
        else:
            input_names = self.predictor.get_input_names()
            for i in range(len(inputs)):
                input_tensor = self.predictor.get_input_tensor(input_names[i])
                input_tensor.copy_from_cpu(inputs[input_names[i]])

            for i in range(warmup):
                self.predictor.zero_copy_run()
                output_names = self.predictor.get_output_names()
                boxes_tensor = self.predictor.get_output_tensor(output_names[0])
                np_boxes = boxes_tensor.copy_to_cpu()
                if self.config.mask_resolution is not None:
                    masks_tensor = self.predictor.get_output_tensor(
                        output_names[1])
                    np_masks = masks_tensor.copy_to_cpu()

            # t1 = time.time()
            for i in range(repeats):
                self.predictor.zero_copy_run()
                output_names = self.predictor.get_output_names()
                boxes_tensor = self.predictor.get_output_tensor(output_names[0])
                np_boxes = boxes_tensor.copy_to_cpu()
                if self.config.mask_resolution is not None:
                    masks_tensor = self.predictor.get_output_tensor(
                        output_names[1])
                    np_masks = masks_tensor.copy_to_cpu()
            # t2 = time.time()
            # ms = (t2 - t1) * 1000.0 / repeats
            # print("Inference: {} ms per batch image".format(ms))

        if reduce(lambda x, y: x * y, np_boxes.shape) < 6:
            print('[WARNNING] No object detected.')
            results = {'boxes': np.array([])}
        else:
            results = self.postprocess(
                np_boxes, np_masks, im_info, threshold=threshold)

        return results

    def predict_a_frame(self, frame, threshold=0.2):
        results = self.predict(frame, threshold)
        return results


def predict_image(model_dir, images_dir, threshold=0.2, use_gpu=True, run_mode='fluid', output_dir='output/'):
    detector = Detector(model_dir, use_gpu=use_gpu, run_mode=run_mode)
    # if FLAGS.run_benchmark:
    #     detector.predict(
    #         FLAGS.image_file, FLAGS.threshold, warmup=100, repeats=100)
    # else:
    files = os.listdir(images_dir)

    for file in files:
        p1, p2 = os.path.splitext(file)
        if p2 == ".png" or p2 == ".jpg":
            image_file = images_dir + '/' + file
            results = detector.predict(image_file, threshold)
            print(results)
            visualize(
                image_file,
                results,
                detector.config.labels,
                mask_resolution=detector.config.mask_resolution,
                output_dir=output_dir)


def predict_video(model_dir, video_file="0", threshold=0.2, use_gpu=True, run_mode='fluid', output_dir='output/'):
    if video_file == "0":
        video_file = 0
    detector = Detector(model_dir, use_gpu=use_gpu, run_mode=run_mode)
    capture = cv2.VideoCapture(video_file)
    fps = 25
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video_name = 'camera.mp4'
    if video_file != 0:
        video_name = os.path.split(video_file)[-1]
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    out_path = os.path.join(output_dir, video_name)
    writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    index = 1
    while 1:
        ret, frame = capture.read()
        if not ret:
            break
        print('detect frame:%d' % index)
        index += 1
        results = detector.predict(frame, threshold)
        im = visualize_box_mask(
            frame,
            results,
            detector.config.labels,
            mask_resolution=detector.config.mask_resolution)
        im = np.array(im)
        cv2.imshow("dsf", im)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
        writer.write(im)
    writer.release()


def pirun(model_dir, IP="192.168.43.174", port=8787, threshold=0.2, use_gpu=True, run_mode='fluid',
          output_dir='output/'):
    # 1. scan and get all images with valid extensions in directory imgs_dir
    # 2. create a predictor#预测er

    # 视频来源，可以来自一段已存好的视频，也可以直接来自USB摄像头
    cv2.namedWindow('video', 0)
    cv2.resizeWindow('video', 640, 480)

    send_q = Queue()
    recv_q = Queue()

    predicter = BackgroundPredict(model_dir, recv_q, send_q, threshold=threshold, use_gpu=use_gpu, run_mode=run_mode,
                                  output_dir=output_dir)
    from pi_server import VideoStreamingTest
    videoStreaming = VideoStreamingTest(IP, port)
    predicter.start()
    stream_bytes = b' '
    n = 1
    while True:
        stream_bytes += videoStreaming.reader.read(1024)
        first = stream_bytes.find(b'\xff\xd8')
        last = stream_bytes.find(b'\xff\xd9')
        if first != -1 and last != -1:
            jpg = stream_bytes[first:last + 2]
            stream_bytes = stream_bytes[last + 2:]
            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if n == 1:
                send_q.put(frame)
                n = 0
            # print('runing')
            if not recv_q.empty():
                # print("frame", frame)
                g = recv_q.get()
                send_q.put(frame)
            cv2.imshow('video', frame)

            c = cv2.waitKey(10)
            if c & 0xFF == ord('q'):
                send_q.put(0)
                break
    cv2.destroyAllWindows()
    return 0


class BackgroundPredict(Process):
    def __init__(self, model_dir, recvq, sendq, threshold=0.2, use_gpu=True, run_mode='fluid', output_dir='output/'):
        super().__init__()
        # self.frame = frame
        # try:
        #     if frame is None:
        #         self.predicterfree = True
        #     else:
        #         self.predicterfree = False
        # except:
        self.threshold = threshold
        self.output_dir = output_dir
        self.predicterfree = True
        self.use_gpu = use_gpu
        self.run_mode = run_mode
        self.model_dir = model_dir
        self.isOpened = True
        self.sendq = recvq
        self.recvq = sendq

    def run(self):
        self.detector = Detector(self.model_dir, use_gpu=self.use_gpu, run_mode=self.run_mode)
        self.videoWriter = cv2.VideoWriter('out.mp4', cv2.VideoWriter_fourcc('I', '4', '2', '0'), 10, (480, 320))
        cv2.namedWindow('mask', 0)
        cv2.resizeWindow('mask', 640, 480)
        self.sendq.put(0)
        while self.isOpened:
            if self.predicterfree:
                if not self.recvq.empty():
                    self.frame = self.recvq.get()
                    if type(self.frame) == int:
                        break
                    # cv2.imwrite(r'D:/python_work/Ar_project/PaddleSeg/dataset/test/{}.png'.format(time.time()),self.frame)
                    self.predicterfree = False
                    # a = self.sendq.recv()
                    print("child got a frame")
                # else:
                #
                #     print("child is waiting a frame")
            else:
                t0 = time.time()
                result = self.detector.predict(self.frame, self.threshold)
                print("预测时间:", time.time() - t0)
                # print(result)
                pi_visualize(
                    self.frame,
                    result,
                    self.detector.config.labels,
                    mask_resolution=self.detector.config.mask_resolution,
                    video_writer=self.videoWriter)
                self.predicterfree = True
                self.sendq.put(0)
        self.videoWriter.release()


if __name__ == '__main__':
    # global ui_window
    # app = QApplication(sys.argv)
    # os.chdir('D:/python_work/Ar_project/PaddleDetection')
    model_dir = r"D:\python_work\Ar_project\PaddleDetection\inference_model\yolov3_mobilenet_v3ndd000"
    # ui_window = ArMainwindow()
    # 预测图片
    # preditcter=Detector(model_dir,True)
    # result=preditcter.predict_a_frame(cv2.imread(r"D:\python_work\Ar_project\testpp.png"))
    # print(result)
    # predict_image(model_dir, "D:/python_work/Ar_project/PaddleDetection/te", use_gpu=True)
    # 预测视频/自带摄像头(一个)
    # predict_video(model_dir, video_file=r"D:\python_work\Ar_project\video\nvideo.mp4",use_gpu=False)
    # 预测pi摄像头
    pirun(model_dir, IP="192.168.137.1")
    # sys.exit(app.exec_())
