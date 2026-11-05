"""Metric occupancy grids and frontier stations on reachable, known free space."""
from dataclasses import dataclass
import hashlib
import math
from collections import deque
import numpy as np
from scipy import ndimage


def finite(value, name, lower=-math.inf, upper=math.inf):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(name + ' 无效')
    return float(value)


def polygon(value):
    if value is None or value == []: return None
    if not isinstance(value, list) or not 3 <= len(value) <= 100: raise ValueError('区域需 3–100 个顶点')
    points = [(finite(p[0], 'x'), finite(p[1], 'y')) for p in value if isinstance(p, (list, tuple)) and len(p) == 2]
    if len(points) != len(value): raise ValueError('区域坐标格式无效')
    def cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def intersects(a,b,c,d):
        if max(a[0],b[0]) < min(c[0],d[0]) or max(c[0],d[0]) < min(a[0],b[0]) or max(a[1],b[1]) < min(c[1],d[1]) or max(c[1],d[1]) < min(a[1],b[1]): return False
        return cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0
    for i in range(len(points)):
        for j in range(i+1,len(points)):
            if j in (i,(i+1)%len(points)) or i == (j+1)%len(points): continue
            if intersects(points[i],points[(i+1)%len(points)],points[j],points[(j+1)%len(points)]): raise ValueError('区域不能自交')
    area = sum(points[i][0]*points[(i+1)%len(points)][1]-points[(i+1)%len(points)][0]*points[i][1] for i in range(len(points)))
    if abs(area) < 1e-6: raise ValueError('区域面积为零')
    return points


@dataclass
class Grid:
    data: np.ndarray
    resolution: float
    origin: tuple = (0., 0., 0.)
    map_id: str = 'simulation'

    def __post_init__(self):
        raw = np.asarray(self.data)
        if raw.ndim != 2 or not all(raw.shape) or raw.size > 4_000_000: raise ValueError('地图尺寸无效')
        if not np.isfinite(raw).all() or not ((raw >= -1) & (raw <= 100) & (raw == np.floor(raw))).all(): raise ValueError('地图占据值无效')
        self.data = raw.astype(np.int8, copy=True)
        self.resolution = finite(self.resolution, 'resolution', .001, 10)
        if len(self.origin) != 3: raise ValueError('origin 需要 x/y/yaw')
        self.origin = tuple(finite(x, 'origin') for x in self.origin)
        if not isinstance(self.map_id,str) or not self.map_id: raise ValueError('map_id 无效')

    @property
    def width(self): return self.data.shape[1]
    @property
    def height(self): return self.data.shape[0]
    @property
    def version(self):
        h=hashlib.sha256(self.data.tobytes());h.update(str((self.resolution,self.origin,self.map_id)).encode());return h.hexdigest()[:20]
    def world(self, col, row):
        x=(col+.5)*self.resolution;y=(row+.5)*self.resolution;c=math.cos(self.origin[2]);s=math.sin(self.origin[2])
        wx,wy=self.origin[0]+c*x-s*y,self.origin[1]+s*x+c*y
        return (float(wx),float(wy)) if np.ndim(wx)==0 else (wx,wy)
    def cell(self, x, y):
        x=finite(x,'x')-self.origin[0];y=finite(y,'y')-self.origin[1];c=math.cos(self.origin[2]);s=math.sin(self.origin[2])
        return math.floor((c*x+s*y)/self.resolution),math.floor((-s*x+c*y)/self.resolution)
    def meta(self):
        return dict(map_id=self.map_id,version=self.version,width=self.width,height=self.height,resolution=self.resolution,origin=list(self.origin),frame_id='map',known_cells=int((self.data>=0).sum()),known_area_m2=round(float((self.data>=0).sum()*self.resolution**2),3))
    @classmethod
    def from_ros(cls, msg, map_id):
        info=msg['info'];w=info['width'];h=info['height']
        if type(w) is not int or type(h) is not int or w<=0 or h<=0 or w*h>4_000_000 or len(msg['data'])!=w*h: raise ValueError('ROS 地图长度无效')
        p=info['origin']['position'];q=info['origin']['orientation'];norm=sum(q[k]**2 for k in ('x','y','z','w'))
        if not math.isfinite(norm) or abs(norm-1)>1e-3 or abs(q['x'])>1e-4 or abs(q['y'])>1e-4: raise ValueError('地图原点不是有效平面姿态')
        yaw=math.atan2(2*q['w']*q['z'],1-2*q['z']**2)
        return cls(np.asarray(msg['data']).reshape(h,w),info['resolution'],(p['x'],p['y'],yaw),map_id)


def region_mask(grid, boundary=None, exclusions=None):
    yy,xx=np.indices(grid.data.shape);x,y=grid.world(xx,yy)
    def inside(points):
        result=np.zeros(grid.data.shape,dtype=bool)
        for a,b in zip(points,points[1:]+points[:1]):
            if a[1] == b[1]: continue
            result ^= ((a[1]>y)!=(b[1]>y)) & (x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0])
        return result
    mask=np.ones(grid.data.shape,dtype=bool) if boundary is None else inside(polygon(boundary))
    for points in exclusions or []:mask &= ~inside(polygon(points))
    return mask


class Search:
    def __init__(self, grid, pose, radius, boundary=None, exclusions=None):
        self.grid=grid;self.pose=pose;self.region=region_mask(grid,boundary,exclusions)
        free=(grid.data>=0)&(grid.data<=19)&self.region
        # Padding treats map edges and region edges as unknown/non-traversable.
        clearance=ndimage.distance_transform_edt(np.pad(free,1))[1:-1,1:-1]*grid.resolution
        self.safe=free & (clearance>finite(radius,'radius',.01,3)+grid.resolution*math.sqrt(2)/2)
        self.start=grid.cell(pose[0],pose[1]);col,row=self.start
        self.distance=np.full(free.shape,-1,dtype=np.int32);self.parent={}
        if not (0<=col<grid.width and 0<=row<grid.height) or not self.safe[row,col]: raise ValueError('当前位姿不在具有机身净空的已知自由区域')
        queue=deque([(col,row)]);self.distance[row,col]=0
        while queue:
            x,y=queue.popleft()
            for nx,ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if 0<=nx<grid.width and 0<=ny<grid.height and self.safe[ny,nx] and self.distance[ny,nx]<0:
                    self.distance[ny,nx]=self.distance[y,x]+1;self.parent[(nx,ny)]=(x,y);queue.append((nx,ny))

    def path(self, x, y):
        col,row=self.grid.cell(x,y)
        if not (0<=col<self.grid.width and 0<=row<self.grid.height) or self.distance[row,col]<0: raise ValueError('目标无已知自由空间内的可达路径')
        cells=[(col,row)]
        while cells[-1]!=self.start:cells.append(self.parent[cells[-1]])
        return [list(self.grid.world(*cell)) for cell in reversed(cells)]

    def frontiers(self, excluded=(), min_size=.3):
        g=self.grid
        frontier=(g.data>=0)&(g.data<=19)&self.region&ndimage.binary_dilation(g.data<0,structure=ndimage.generate_binary_structure(2,1))
        labels,count=ndimage.label(frontier,structure=np.ones((3,3)))
        result=[]
        for label in range(1,count+1):
            cluster=labels==label;size=int(cluster.sum())
            if size*g.resolution<min_size:continue
            to_edge=ndimage.distance_transform_edt(~cluster)*g.resolution
            valid=(self.distance>=0)&(to_edge<=max(.65,3*g.resolution))&(self.distance*g.resolution>=.25)
            for ex in excluded:
                yy,xx=np.indices(g.data.shape);wx,wy=g.world(xx,yy);valid &= ((wx-ex[0])**2+(wy-ex[1])**2)>.35**2
            if not valid.any():continue
            costs=np.where(valid,to_edge+.15*self.distance*g.resolution,np.inf)
            row,col=np.unravel_index(np.argmin(costs),costs.shape);x,y=g.world(col,row)
            edge=np.argwhere(cluster);nearest=edge[np.argmin((edge[:,0]-row)**2+(edge[:,1]-col)**2)];tx,ty=g.world(nearest[1],nearest[0])
            yaw=math.atan2(ty-y,tx-x);distance=float(self.distance[row,col]*g.resolution)
            score=size*g.resolution-.8*distance-.15*abs(math.atan2(math.sin(yaw-self.pose[2]),math.cos(yaw-self.pose[2])))
            result.append(dict(x=x,y=y,yaw=yaw,frontier_cells=size,path_m=round(distance,3),score=round(score,3)))
        return sorted(result,key=lambda f:f['score'],reverse=True)
