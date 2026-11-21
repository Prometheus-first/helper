"""Retargeting geometry and tracking-loss behavior without a camera or GPU."""
import importlib.util
import unittest

from wxbot_framework.body_pose import BodyPoseTracker,HeadPoseTracker


@unittest.skipUnless(importlib.util.find_spec('numpy'), 'Optional AI runtime NumPy')
class BodyPoseTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np=np
        self.tracker=BodyPoseTracker(np.zeros((22,3)))

    def test_opposite_vectors_produce_a_rotation_without_nan(self):
        np=self.np
        rotation=self.tracker._rotation_between([1,0,0],[-1,0,0])
        np.testing.assert_allclose(rotation@np.array([1,0,0]),[-1,0,0],atol=1e-8)
        np.testing.assert_allclose(rotation.T@rotation,np.eye(3),atol=1e-8)
        self.assertAlmostEqual(float(np.linalg.det(rotation)),1.)
        self.assertAlmostEqual(float(np.linalg.norm(self.tracker._axis_angle(rotation))),np.pi)

    def test_missing_nonfinite_or_implausible_landmarks_hold_the_last_pose(self):
        np=self.np
        expected=self.tracker.pose.copy()
        for data in ([],np.zeros((33,4)),np.full((33,4),np.nan),np.full((33,4),10.)):
            pose,root,tracked=self.tracker.update(data)
            self.assertFalse(tracked)
            np.testing.assert_array_equal(pose,expected)
            self.assertTrue(np.isfinite(root).all())

    def test_collapsed_bones_do_not_generate_invalid_rotations(self):
        np=self.np
        np.testing.assert_array_equal(self.tracker._rotation_between([0,0,0],[1,0,0]),np.eye(3))

    def test_rest_skeleton_is_validated_before_tracking(self):
        with self.assertRaises(ValueError):
            BodyPoseTracker([[0,0,0]])

    def test_raising_one_arm_changes_its_shoulder_without_turning_the_pelvis(self):
        np=self.np
        rest=np.array([[0,0,0],[.1,-.1,0],[-.1,-.1,0],[0,.1,0],
            [.1,-.5,0],[-.1,-.5,0],[0,.3,0],[.1,-.9,0],[-.1,-.9,0],
            [0,.5,0],[.1,-.9,.1],[-.1,-.9,.1],[0,.6,0],[.1,.5,0],[-.1,.5,0],
            [0,.8,0],[.2,.5,0],[-.2,.5,0],[.5,.5,0],[-.5,.5,0],[.8,.5,0],[-.8,.5,0]])
        tracker=BodyPoseTracker(rest)
        values=np.zeros((33,4));values[:,3]=1
        mapping={23:1,24:2,25:4,26:5,27:7,28:8,31:10,32:11,
                 11:16,12:17,13:18,14:19,15:20,16:21}
        for landmark,joint in mapping.items():
            values[landmark,:3]=rest[joint]*[1,-1,-1]
        values[7,:3]=[.05,-.8,0];values[8,:3]=[-.05,-.8,0]
        for _ in range(12):tracker.update(values)
        values[13,:3]=[.2,-.8,0];values[15,:3]=[.2,-1.1,0]
        pose,root,tracked=tracker.update(values)
        self.assertTrue(tracked)
        self.assertGreater(pose[15][2],.5)
        self.assertLess(np.linalg.norm(pose[16]),.01)
        self.assertLess(np.linalg.norm(root),.01)

    def test_head_only_rotation_keeps_limbs_and_root_fixed(self):
        np=self.np
        tracker=HeadPoseTracker()
        tracker.update_matrix(np.eye(4))
        angle=.4
        matrix=np.eye(4)
        matrix[:3,:3]=[[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]]
        pose,root,tracked=tracker.update_matrix(matrix)
        self.assertTrue(tracked)
        np.testing.assert_allclose(np.array(pose)[11]+np.array(pose)[14],[0,angle*.45,0],atol=1e-6)
        self.assertEqual(pose[15],[0,0,-1])
        self.assertEqual(pose[16],[0,0,1])
        self.assertEqual(root,[0,0,0])
        self.assertFalse(tracker.update_matrix([])[2])
        tracker.calibrate()
        pose,_,_=tracker.update_matrix(matrix)
        np.testing.assert_allclose(np.array(pose)[11]+np.array(pose)[14],[0,0,0],atol=1e-6)


if __name__=='__main__':
    unittest.main()
