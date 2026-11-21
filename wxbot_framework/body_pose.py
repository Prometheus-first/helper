"""Optional NumPy retargeting from MediaPipe world landmarks to SMPL-X bones."""
import math


class BodyPoseTracker:
    parents = (-1,0,0,0,1,2,3,4,5,6,7,8,9,9,9,12,13,14,16,17,18,19)
    children = {1:4,2:5,3:6,4:7,5:8,6:9,7:10,8:11,9:12,12:15,13:16,14:17,16:18,17:19,18:20,19:21}

    def __init__(self, rest_joints):
        import numpy as np
        self.np = np
        self.rest = np.asarray(rest_joints,dtype=float)
        if self.rest.shape != (22,3) or not np.isfinite(self.rest).all():
            raise ValueError('Invalid avatar rest skeleton')
        self.pose = np.zeros((21,3))
        self.pose[15,2],self.pose[16,2] = -1.,1.
        self.root = np.zeros(3)
        self._reference_root = None

    def _rotation_between(self, a, b):
        np = self.np
        a,b = np.asarray(a,float),np.asarray(b,float)
        an,bn = np.linalg.norm(a),np.linalg.norm(b)
        if min(an,bn)<1e-8:
            return np.eye(3)
        a,b = a/an,b/bn
        cosine = float(np.clip(a@b,-1,1))
        if cosine < -1+1e-7:
            axis = np.cross(a,np.eye(3)[np.argmin(abs(a))])
            axis /= np.linalg.norm(axis)
            return 2*np.outer(axis,axis)-np.eye(3)
        v = np.cross(a,b)
        cross = np.array([[0,-v[2],v[1]],[v[2],0,-v[0]],[-v[1],v[0],0]])
        return np.eye(3)+cross+cross@cross/(1+cosine)

    def _axis_angle(self, rotation):
        np = self.np
        angle = math.acos(float(np.clip((np.trace(rotation)-1)/2,-1,1)))
        if angle<1e-7:
            return np.zeros(3)
        if math.pi-angle<1e-5:
            values,vectors=np.linalg.eigh((rotation+np.eye(3))/2)
            axis=vectors[:,values.argmax()]
            return axis*angle
        axis=np.array([rotation[2,1]-rotation[1,2],rotation[0,2]-rotation[2,0],rotation[1,0]-rotation[0,1]])
        return axis*(angle/(2*math.sin(angle)))

    def calibrate(self):
        self._reference_root=None

    def update(self, landmarks):
        np=self.np
        values=np.asarray(landmarks,dtype=float)
        if values.shape!=(33,4) or not np.isfinite(values).all() or abs(values[:,:3]).max()>5:
            return self.pose.tolist(),self.root.tolist(),False
        visible=values[:,3]
        if min(visible[[11,12,23,24]])<.35:
            return self.pose.tolist(),self.root.tolist(),False
        world=values[:,:3]*np.array([1.,-1.,-1.])
        pelvis=(world[23]+world[24])/2
        shoulders=(world[11]+world[12])/2
        head=(world[7]+world[8])/2
        target={0:pelvis,1:world[23],2:world[24],3:pelvis+(shoulders-pelvis)/3,
                6:pelvis+2*(shoulders-pelvis)/3,9:shoulders,
                12:shoulders+.35*(head-shoulders),15:head,
                13:(shoulders+world[11])/2,14:(shoulders+world[12])/2}
        mapping={4:25,5:26,7:27,8:28,10:31,11:32,16:11,17:12,18:13,19:14,20:15,21:16}
        target.update({joint:world[index] for joint,index in mapping.items()})
        axis_x=world[23]-world[24]
        axis_x/=max(np.linalg.norm(axis_x),1e-8)
        axis_z=np.cross(axis_x,[0.,1.,0.])
        if np.linalg.norm(axis_z)<.1:
            return self.pose.tolist(),self.root.tolist(),False
        axis_z/=np.linalg.norm(axis_z)
        rotation=np.stack([axis_x,np.cross(axis_z,axis_x),axis_z],axis=1)
        if self._reference_root is None:
            self._reference_root=rotation.copy()
        root=self._axis_angle(self._reference_root.T@rotation)
        root*=min(1.,1.2/max(np.linalg.norm(root),1e-8))
        global_rotations={0:rotation}
        pose=np.zeros((21,3))
        confidence={joint:visible[index] for joint,index in mapping.items()}
        for joint in range(1,22):
            parent=self.parents[joint]
            parent_rotation=global_rotations[parent]
            child=self.children.get(joint)
            if child is not None and min(confidence.get(joint,1.),confidence.get(child,1.))>=.4:
                local=self._rotation_between(self.rest[child]-self.rest[joint],
                    parent_rotation.T@(target[child]-target[joint]))
                pose[joint-1]=self._axis_angle(local)
            else:
                local=np.eye(3)
                if child is not None:
                    pose[joint-1]=self.pose[joint-1]
                    angle=np.linalg.norm(pose[joint-1])
                    if angle>1e-8:
                        axis=pose[joint-1]/angle
                        k=np.array([[0,-axis[2],axis[1]],[axis[2],0,-axis[0]],[-axis[1],axis[0],0]])
                        local=np.eye(3)+math.sin(angle)*k+(1-math.cos(angle))*k@k
            global_rotations[joint]=parent_rotation@local
        self.pose=.55*self.pose+.45*pose
        self.root=.55*self.root+.45*root
        return self.pose.tolist(),self.root.tolist(),True


class HeadPoseTracker(BodyPoseTracker):
    """Face-only tracking keeps the body fixed and distributes rotation across neck/head."""
    def __init__(self):
        import numpy as np
        super().__init__(np.zeros((22,3)))
        self._head_reference=None
        self._head=np.zeros(3)

    def calibrate(self):
        self._head_reference=None
        self._head[:]=0

    def update_matrix(self, matrix):
        np=self.np
        value=np.asarray(matrix,dtype=float)
        if value.shape not in ((3,3),(4,4)) or not np.isfinite(value).all():
            return self.pose.tolist(),[0.,0.,0.],False
        u,scale,vt=np.linalg.svd(value[:3,:3])
        if min(scale)<1e-6:
            return self.pose.tolist(),[0.,0.,0.],False
        rotation=u@vt
        if np.linalg.det(rotation)<0:
            u[:,-1]*=-1
            rotation=u@vt
        if self._head_reference is None:
            self._head_reference=rotation.copy()
        delta=self._axis_angle(self._head_reference.T@rotation)
        delta*=min(1.,.7/max(np.linalg.norm(delta),1e-8))
        self._head=.55*self._head+.45*delta
        self.pose[11]=self._head*.3
        self.pose[14]=self._head*.7
        return self.pose.tolist(),[0.,0.,0.],True
