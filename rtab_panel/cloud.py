"""Display measured RGB-D in RTAB camera poses; never infer unobserved surfaces."""
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image


def project(depth, rgb, intrinsics, pose, stride=4):
    _, _, fx, fy, cx, cy = intrinsics
    v, u = np.mgrid[0:depth.shape[0]:stride, 0:depth.shape[1]:stride]
    z = depth[::stride, ::stride].astype(float) * .001
    valid = (z >= .15) & (z <= 5.)
    xyz = np.stack(((u-cx)*z/fx, (v-cy)*z/fy, z), axis=-1)[valid]
    return xyz @ pose[:3, :3].T + pose[:3, 3], rgb[::stride, ::stride][valid]


def rows(path):
    if not path.exists():
        return
    with path.open() as stream:
        next(stream, None)
        for line in stream:
            if line.endswith('\n') and line.strip():
                yield line.rstrip('\n').split('\t')


class Cloud:
    def __init__(self, trajectory_limit=10000):
        self.last_id = 0
        self.tracked = 0
        self.lost = 0
        self.trajectory = deque(maxlen=trajectory_limit)
        self.tracking_offset = 0
        self.voxels = {}
        self.anchor = None
        self.camera_pose = None
        self.basis = 'odometry'
        self.version = 0
        self.optimized = False

    def tracking_rows(self, path):
        if not path.exists():
            return
        with path.open() as stream:
            stream.seek(self.tracking_offset)
            while True:
                line = stream.readline()
                if not line.endswith('\n'):
                    break
                self.tracking_offset = stream.tell()
                if line.strip() and not line.startswith('id\t'):
                    yield line.rstrip('\n').split('\t')

    def add(self, root, identity, pose, intrinsics, force=False):
        self.trajectory.append(pose[:3, 3].round(4).tolist())
        self.camera_pose = pose.tolist()
        if not force and self.anchor is not None:
            distance = np.linalg.norm(pose[:3, 3]-self.anchor[:3, 3])
            rotation = np.linalg.norm(pose[:3, :3]-self.anchor[:3, :3])
            if distance < .04 and rotation < .06:
                return
        name = f'{identity:06d}.png'
        with Image.open(root/'input/rgb'/name) as image:
            rgb = np.asarray(image)
        with Image.open(root/'input/depth'/name) as image:
            depth = np.asarray(image)
        points, colors = project(depth, rgb, intrinsics, pose)
        for point, color in zip(points, colors):
            key = tuple(np.floor(point/.04).astype(int))
            if key in self.voxels or len(self.voxels) < 45000:
                self.voxels[key] = point.round(3).tolist()+color.tolist()
        self.anchor = pose

    def update(self, run, closed=False):
        root = Path(run)/'slam'
        calibration = root/'input/camera.txt'
        if not calibration.exists():
            return
        intrinsics = [float(x) for x in calibration.read_text().split()]
        for row in self.tracking_rows(root/'baseline/tracking.tsv'):
            identity = int(row[0])
            if identity <= self.last_id:
                continue
            if row[2] == 'lost':
                self.lost += 1
            elif row[2] == 'tracked' and row[3] == '1':
                pose = np.eye(4)
                pose[:3] = np.asarray(row[6:], dtype=float).reshape(3, 4)
                self.add(root, identity, pose, intrinsics)
                self.tracked += 1
            self.last_id = identity
            self.version += 1
        if closed and not self.optimized:
            found = False
            for row in rows(root/'baseline/optimized-camera-poses.tsv'):
                if not found:
                    self.voxels.clear()
                    self.trajectory.clear()
                    self.anchor = None
                    found = True
                pose = np.eye(4)
                pose[:3] = np.asarray(row[1:], dtype=float).reshape(3, 4)
                self.add(root, int(row[0]), pose, intrinsics, force=True)
            if found:
                self.basis = 'optimized'
                self.version += 1
            self.optimized = True

    def snapshot(self):
        return {'points': list(self.voxels.values()), 'trajectory': list(self.trajectory),
                    'camera_pose': self.camera_pose,
                    'tracked_frames': self.tracked, 'lost_frames': self.lost, 'version': self.version,
                    'basis': self.basis, 'point_limit': 45000, 'voxel_m': .04,
                    'trajectory_limit': self.trajectory.maxlen,
                    'coordinates': 'RTAB map: x forward, y left, z up; metres'}
