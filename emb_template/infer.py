import argparse

import cv2
import numpy as np
import torch
import rospy
from transform3d import Transform

from . import utils
from . import vis
from .camera import Camera, CameraInfo
from .model import Model
from .renderer import MeshRenderer

parser = argparse.ArgumentParser()
parser.add_argument('object_name')
parser.add_argument('--from-file')
parser.add_argument('--show-template', action='store_true')
parser.add_argument('--show-embedding', action='store_true')
parser.add_argument('--show-activation', action='store_true')
parser.add_argument('--show-certainty', action='store_true')
parser.add_argument('--show-angle-dist', action='store_true')
args = parser.parse_args()
object_name = args.object_name

cam_t_table = Transform.load('cam_t_table.txt')
rgba_template, table_offset, obj_t_template, sym = utils.load_current_template(object_name)
cam_info = CameraInfo.load()

model = Model.load_from_checkpoint(
    # TODO: load *current* model
    utils.latest_checkpoint(object_name),
    rgba_template=rgba_template, sym=sym
)
model.eval()
model.cuda()
img_scale = model.img_scale
rgba_template = utils.resize(rgba_template, img_scale)[0]

img_temp, M = utils.resize(np.zeros((cam_info.h, cam_info.w, 3)), img_scale)
h, w = img_temp.shape[:2]
K = M @ cam_info.K
renderer = MeshRenderer(
    mesh=utils.load_mesh(object_name), h=h, w=w, K=K,
)

if args.from_file is not None:
    def get_img():
        return cv2.imread(args.from_file)
else:
    rospy.init_node('infer', anonymous=True)
    cam = Camera()


    def get_img():
        return cam.take_image()

if args.show_template:
    template = model.get_template()[0]
    template_img = np.concatenate((
        vis.premultiply_alpha(rgba_template),
        vis.emb_for_vis(template),
    ), axis=1)
    cv2.imshow('template', template_img)
    print(f'template mean norm: {template.norm(dim=1).mean():0.3e}')

print('Press Space or Enter to save the current image for annotation.\n'
      '"r" to switch between 3D pose render and template overlay')

do_render = True
hidden = False

while True:
    img_full = get_img()
    img = utils.resize(img_full, img_scale)[0]
    img_ = utils.normalize(img).to(model.device)
    with torch.no_grad():
        act, emb = model.forward(img_[None])
        act, emb = act[0], emb[0]
    if args.show_activation:
        cv2.imshow('act', vis.overlay_activation_2d(img, act, model.stride))
    if args.show_embedding:
        emb_img = vis.emb_for_vis(emb).copy()
        cv2.putText(emb_img, f'mean norm: {emb.norm(dim=1).mean():0.3e}', (0, 12),
                    cv2.FONT_HERSHEY_PLAIN, 1., (255, 255, 255))
        cv2.imshow('emb', emb_img)
    probs = torch.softmax(act.view(-1), 0).view(*act.shape)
    certainty = probs.max().item()

    act = act.cpu().numpy()
    pose_2d = utils.pose_2d_from_act(act=act, stride=model.stride, sym=sym)
    cam_t_obj = utils.get_pose_3d(
        pose_2d=pose_2d, K=K, cam_t_table=cam_t_table,
        table_offset=table_offset, obj_t_template=obj_t_template,
    )

    if hidden:
        img_overlay = img.copy()
    else:
        if do_render:
            render = renderer.render(cam_t_obj)[0].copy()
            render[..., :2] = 0
            img_overlay = vis.composite(img, render[..., :3], render[..., 3:] // 2)
        else:
            img_overlay = vis.overlay_template(img, rgba_template, *pose_2d)
    cv2.putText(img_overlay, f'certainty: {certainty:.2f}', (0, 12), cv2.FONT_HERSHEY_PLAIN, 1, (0, 0, 255))
    cv2.imshow('', img_overlay)
    key = cv2.waitKey(1)
    if key == ord('q'):
        break
    elif key == ord('\r') or key == ord(' '):
        utils.log_prediction(object_name=object_name, img=img_full, cam_t_obj=cam_t_obj)
        print('Image saved in logs')
    elif key == ord('r'):
        do_render = not do_render
    elif key == ord('h'):
        hidden = not hidden
