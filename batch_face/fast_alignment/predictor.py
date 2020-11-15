# Face alignment demo
# Uses MTCNN as face detector
# Cunjian Chen (ccunjian@gmail.com)
from batch_face.fast_alignment.utils import load_weights
import torch
import cv2
import numpy as np
from torch.utils.data import DataLoader
from .basenet import MobileNet_GDConv
from .pfld_compressed import PFLDInference


def get_device(gpu_id):
    if gpu_id > -1:
        return torch.device(f"cuda:{str(gpu_id)}")
    else:
        return torch.device("cpu")


# landmark of (5L, 2L) from [0,1] to real range
def reproject(bbox, landmark):
    landmark_ = landmark.clone()
    x1, y1, x2, y2 = bbox
    w = x2 - x1
    h = y2 - y1
    landmark_[:, 0] *= w
    landmark_[:, 0] += x1
    landmark_[:, 1] *= h
    landmark_[:, 1] += y1
    return landmark_


def prepare_feed(img, face, backbone):
    if backbone == "MobileNet":
        out_size = 224
    else:
        out_size = 112
    mean = np.asarray([0.485, 0.456, 0.406])
    std = np.asarray([0.229, 0.224, 0.225])
    x1 = face[0]
    y1 = face[1]
    x2 = face[2]
    y2 = face[3]
    height, width, _ = img.shape
    w = x2 - x1 + 1
    h = y2 - y1 + 1
    size = int(min([w, h]) * 1.2)
    cx = x1 + w // 2
    cy = y1 + h // 2
    x1 = cx - size // 2
    x2 = x1 + size
    y1 = cy - size // 2
    y2 = y1 + size

    dx = max(0, -x1)
    dy = max(0, -y1)
    x1 = max(0, x1)
    y1 = max(0, y1)

    edx = max(0, x2 - width)
    edy = max(0, y2 - height)
    x2 = min(width, x2)
    y2 = min(height, y2)
    new_bbox = torch.Tensor([x1, y1, x2, y2]).int()
    x1, y1, x2, y2 = new_bbox
    cropped = img[y1:y2, x1:x2]
    if dx > 0 or dy > 0 or edx > 0 or edy > 0:
        cropped = cv2.copyMakeBorder(
            cropped, int(dy), int(edy), int(dx), int(edx), cv2.BORDER_CONSTANT, 0
        )
    cropped_face = cv2.resize(cropped, (out_size, out_size))

    if cropped_face.shape[0] <= 0 or cropped_face.shape[1] <= 0:
        raise NotADirectoryError
    test_face = cropped_face.copy()
    test_face = test_face / 255.0
    if backbone == "MobileNet":
        test_face = (test_face - mean) / std
    test_face = test_face.transpose((2, 0, 1))
    test_face = torch.from_numpy(test_face).float()
    return dict(data=test_face, bbox=new_bbox)


@torch.no_grad()
def single_predict(model, feed, device):
    landmark = model(feed["data"].unsqueeze(0).to(device)).cpu()
    landmark = landmark.reshape(-1, 2)
    landmark = reproject(feed["bbox"], landmark)
    return landmark.numpy()


@torch.no_grad()
def batch_predict(model, feeds, device):
    if not isinstance(feeds, list):
        feeds = [feeds]
    # loader = DataLoader(FeedDataset(feeds), batch_size=50, shuffle=False)
    data = []
    for feed in feeds:
        data.append(feed["data"].unsqueeze(0))
    data = torch.cat(data, 0).to(device)
    results = []

    landmarks = model(data).cpu()
    for landmark, feed in zip(landmarks, feeds):
        landmark = landmark.reshape(-1, 2)
        landmark = reproject(feed["bbox"], landmark)
        results.append(landmark.numpy())
    return results


@torch.no_grad()
def batch_predict2(model, feeds, device, batch_size=None):
    if not isinstance(feeds, list):
        feeds = [feeds]
    if batch_size is None:
        batch_size = len(feeds)
    loader = DataLoader(feeds, batch_size=len(feeds), shuffle=False)
    results = []
    for feed in loader:
        landmarks = model(feed["data"].to(device)).cpu()
        for landmark, bbox in zip(landmarks, feed["bbox"]):
            landmark = landmark.reshape(-1, 2)
            landmark = reproject(bbox, landmark)
            results.append(landmark.numpy())
    return results


def split_feeds(all_feeds, all_faces):
    counts = [len(faces) for faces in all_faces]
    sum_now = 0
    ends = [0]
    for i in range(len(counts)):
        sum_now += counts[i]
        end = sum_now
        ends.append(end)
    return [all_feeds[ends[i - 1] : ends[i]] for i in range(1, len(ends))]


from .utils import detection_adapter, is_image, is_box


class LandmarkPredictor:
    def __init__(self, gpu_id=0, backbone="MobileNet", file=None):
        self.device = get_device(gpu_id)
        self.backbone = backbone
        if backbone == "MobileNet":
            model = MobileNet_GDConv(136)
        elif backbone == "PFLD":
            model = PFLDInference()
        else:
            raise NotADirectoryError(backbone)

        weights = load_weights(file, backbone)

        model.load_state_dict(weights)
        self.model = model.to(self.device).eval()

    def __call__(self, all_boxes, all_images, from_fd=False):
        batch = not is_image(all_images)
        if from_fd:
            all_boxes = detection_adapter(all_boxes, batch=batch)
        if not batch:  # 说明是 1 张图
            if is_box(all_boxes):  # 单张图 单个box
                assert is_image(all_images)
                return self._inner_predict(self.prepare_feed(all_images, all_boxes))
            else:
                feeds = [self.prepare_feed(all_images, box) for box in all_boxes]
                return self._inner_predict(feeds)  # 一张图 多个box
        else:
            assert len(all_boxes) == len(all_images)
            assert is_image(all_images[0])
            return self.batch_predict(all_boxes, all_images)  # 多张图 多个box列表

    def _inner_predict(self, feeds):
        results = batch_predict2(self.model, feeds, self.device)
        if not isinstance(feeds, list):
            results = results[0]
        return results

    def batch_predict(self, all_boxes, all_images):
        all_feeds = []
        for i, (faces, image) in enumerate(zip(all_boxes, all_images)):
            feeds = [self.prepare_feed(image, box) for box in faces]
            all_feeds.extend(feeds)
        all_results = self._inner_predict(all_feeds)
        all_results = split_feeds(all_results, all_boxes)
        return all_results

    def prepare_feed(self, img, face):
        return prepare_feed(img, face, self.backbone)
