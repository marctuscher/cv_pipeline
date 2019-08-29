#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Copyright ©2017. The Regents of the University of California (Regents). All Rights Reserved.
Permission to use, copy, modify, and distribute this software and its documentation for educational,
research, and not-for-profit purposes, without fee and without a signed licensing agreement, is
hereby granted, provided that the above copyright notice, this paragraph and the following two
paragraphs appear in all copies, modifications, and distributions. Contact The Office of Technology
Licensing, UC Berkeley, 2150 Shattuck Avenue, Suite 510, Berkeley, CA 94720-1620, (510) 643-
7201, otl@berkeley.edu, http://ipira.berkeley.edu/industry-info for commercial licensing opportunities.

IN NO EVENT SHALL REGENTS BE LIABLE TO ANY PARTY FOR DIRECT, INDIRECT, SPECIAL,
INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS, ARISING OUT OF
THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF REGENTS HAS BEEN
ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

REGENTS SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE. THE SOFTWARE AND ACCOMPANYING DOCUMENTATION, IF ANY, PROVIDED
HEREUNDER IS PROVIDED "AS IS". REGENTS HAS NO OBLIGATION TO PROVIDE
MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
"""
""" 
ROS Server for planning GQCNN grasps 
Author: Vishal Satish
"""
import json
import math
import numpy as np
import os
import rospy
import time

from cv_bridge import CvBridge, CvBridgeError

from autolab_core import YamlConfig
from gqcnn.grasping import Grasp2D, SuctionPoint2D, CrossEntropyRobustGraspingPolicy, RgbdImageState
from gqcnn.utils import GripperMode, NoValidGraspsException
from perception import CameraIntrinsics, ColorImage, DepthImage, BinaryImage, RgbdImage
from visualization import Visualizer2D as vis

from geometry_msgs.msg import PoseStamped
from std_msgs.msg import Header
from gqcnn.srv import GQCNNGraspPlanner, GQCNNGraspPlannerBoundingBox, GQCNNGraspPlannerSegmask
from gqcnn.msg import GQCNNGrasp

class GraspPlanner(object):
    def __init__(self, cfg, cv_bridge, grasping_policy, grasp_pose_publisher):
        """
        Parameters
        ----------
        cfg : dict
            dictionary of configuration parameters
        cv_bridge: :obj:`CvBridge`
            ROS CvBridge
        grasping_policy: :obj:`GraspingPolicy`
            grasping policy to use
        grasp_pose_publisher: :obj:`Publisher`
            ROS Publisher to publish planned grasp's ROS Pose only for visualization
        """
        self.cfg = cfg
        self.cv_bridge = cv_bridge
        self.grasping_policy = grasping_policy
        self.grasp_pose_publisher = grasp_pose_publisher

        # set min dimensions
        self.pad = max(
            math.ceil(np.sqrt(2) * (float(self.cfg['policy']['metric']['crop_width']) / 2)),
            math.ceil(np.sqrt(2) * (float(self.cfg['policy']['metric']['crop_height']) / 2))
        )        
        self.min_width = 2 * self.pad + self.cfg['policy']['metric']['crop_width']
        self.min_height = 2 * self.pad + self.cfg['policy']['metric']['crop_height']

    def read_images(self, req):
        """ Reads images from a ROS service request
        
        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS ServiceRequest for grasp planner service
        """
        # get the raw depth and color images as ROS Image objects
        raw_color = req.color_image
        raw_depth = req.depth_image

        # get the raw camera info as ROS CameraInfo object
        raw_camera_info = req.camera_info
        
        # wrap the camera info in a perception CameraIntrinsics object
        camera_intr = CameraIntrinsics(raw_camera_info.header.frame_id, raw_camera_info.K[0], raw_camera_info.K[4], raw_camera_info.K[2], raw_camera_info.K[5], raw_camera_info.K[1], raw_camera_info.height, raw_camera_info.width)

        # create wrapped Perception RGB and Depth Images by unpacking the ROS Images using CVBridge ###
        try:
            color_im = ColorImage(self.cv_bridge.imgmsg_to_cv2(raw_color, "rgb8"), frame=camera_intr.frame)
            depth_im = DepthImage(self.cv_bridge.imgmsg_to_cv2(raw_depth, desired_encoding = "passthrough"), frame=camera_intr.frame)
        except CvBridgeError as cv_bridge_exception:
            rospy.logerr(cv_bridge_exception)

        # check image sizes
        if color_im.height != depth_im.height or \
           color_im.width != depth_im.width:
            msg = 'Color image and depth image must be the same shape! Color is %d x %d but depth is %d x %d' %(color_im.height, color_im.width, depth_im.height, depth_im.width)
            rospy.logerr(msg)
            raise rospy.ServiceException(msg)            

        if color_im.height < self.min_height or color_im.width < self.min_width:
            msg = 'Color image is too small! Must be at least %d x %d resolution but the requested image is only %d x %d' %(self.min_height, self.min_width, color_im.height, color_im.width)
            rospy.logerr(msg)
            raise rospy.ServiceException(msg)

        return color_im, depth_im, camera_intr
        
    def plan_grasp(self, req):
        """ Grasp planner request handler .
        
        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS ServiceRequest for grasp planner service
        """
        color_im, depth_im, camera_intr = self.read_images(req)
        return self._plan_grasp(color_im, depth_im, camera_intr)

    def plan_grasp_bb(self, req):
        """ Grasp planner request handler .
        
        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS ServiceRequest for grasp planner service
        """
        color_im, depth_im, camera_intr = self.read_images(req)
        return self._plan_grasp(color_im, depth_im, camera_intr, bounding_box=req.bounding_box)

    def plan_grasp_segmask(self, req):
        """ Grasp planner request handler .
        
        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS ServiceRequest for grasp planner service
        """
        color_im, depth_im, camera_intr = self.read_images(req)
        raw_segmask = req.segmask
        try:
            segmask = BinaryImage(self.cv_bridge.imgmsg_to_cv2(raw_segmask, desired_encoding = "passthrough"), frame=camera_intr.frame)
        except CvBridgeError as cv_bridge_exception:
            rospy.logerr(cv_bridge_exception)
        if color_im.height != segmask.height or \
           color_im.width != segmask.width:
            msg = 'Images and segmask must be the same shape! Color image is %d x %d but segmask is %d x %d' %(color_im.height, color_im.width, segmask.height, segmask.width)
            rospy.logerr(msg)
            raise rospy.ServiceException(msg) 

        return self._plan_grasp(color_im, depth_im, camera_intr, segmask=segmask)    
    
    def _plan_grasp(self, color_im,
                   depth_im,
                   camera_intr,
                   bounding_box=None,
                   segmask=None):
        """ Grasp planner request handler .
        
        Parameters
        ---------
        req: :obj:`ROS ServiceRequest`
            ROS ServiceRequest for grasp planner service
        """
        rospy.loginfo('Planning Grasp')

        # inpaint images
        color_im = color_im.inpaint(rescale_factor=self.cfg['inpaint_rescale_factor'])
        depth_im = depth_im.inpaint(rescale_factor=self.cfg['inpaint_rescale_factor'])

        # init segmask
        if segmask is None:
            segmask = BinaryImage(255*np.ones(depth_im.shape).astype(np.uint8), frame=color_im.frame)
        
        # visualize
        if self.cfg['vis']['color_image']:
            vis.imshow(color_im)
            vis.show()
        if self.cfg['vis']['depth_image']:
            vis.imshow(depth_im)
            vis.show()
        if self.cfg['vis']['segmask'] and segmask is not None:
            vis.imshow(segmask)
            vis.show()

        # aggregate color and depth images into a single perception rgbdimage
        rgbd_im = RgbdImage.from_color_and_depth(color_im, depth_im)

        
        # mask bounding box
        if bounding_box is not None:
            # calc bb parameters
            min_x = bounding_box.minX
            min_y = bounding_box.minY
            max_x = bounding_box.maxX
            max_y = bounding_box.maxY

            # contain box to image->don't let it exceed image height/width bounds
            if min_x < 0:
                min_x = 0
            if min_y < 0:
                min_y = 0
            if max_x > rgbd_im.width:
                max_x = rgbd_im.width
            if max_y > rgbd_im.height:
                max_y = rgbd_im.height

            # mask
            bb_segmask_arr = np.zeros([rgbd_image.height, rgbd_image.width])
            bb_segmask_arr[min_y:max_y, min_x:max_x] = 255
            bb_segmask = BinaryImage(bb_segmask_arr.astype(np.uint8), segmask.frame)
            segmask = segmask.mask_binary(bb_segmask)
        
        # visualize  
        if self.cfg['vis']['rgbd_state']:
            masked_rgbd_im = rgbd_im.mask_binary(segmask)
            vis.figure()
            vis.subplot(1,2,1)
            vis.imshow(masked_rgbd_im.color)
            vis.subplot(1,2,2)
            vis.imshow(masked_rgbd_im.depth)
            vis.show()

        # create an RGBDImageState with the cropped RGBDImage and CameraIntrinsics
        rgbd_state = RgbdImageState(rgbd_im,
                                    camera_intr,
                                    segmask=segmask)
  
        # execute policy
        try:
            return self.execute_policy(rgbd_state, self.grasping_policy, self.grasp_pose_publisher, camera_intr.frame)
        except NoValidGraspsException:
            rospy.logerr('While executing policy found no valid grasps from sampled antipodal point pairs. Aborting Policy!')
            raise rospy.ServiceException('While executing policy found no valid grasps from sampled antipodal point pairs. Aborting Policy!')

    def execute_policy(self, rgbd_image_state, grasping_policy, grasp_pose_publisher, pose_frame):
        """ Executes a grasping policy on an RgbdImageState
        
        Parameters
        ----------
        rgbd_image_state: :obj:`RgbdImageState`
            RgbdImageState from perception module to encapsulate depth and color image along with camera intrinsics
        grasping_policy: :obj:`GraspingPolicy`
            grasping policy to use
        grasp_pose_publisher: :obj:`Publisher`
            ROS Publisher to publish planned grasp's ROS Pose only for visualization
        pose_frame: :obj:`str`
            frame of reference to publish pose alone in
        """
        # execute the policy's action
        grasp_planning_start_time = time.time()
        grasp = grasping_policy(rgbd_image_state)
  
        # create GQCNNGrasp return msg and populate it
        gqcnn_grasp = GQCNNGrasp()
        gqcnn_grasp.q_value = grasp.q_value
        gqcnn_grasp.pose = grasp.grasp.pose().pose_msg
        if isinstance(grasp.grasp, Grasp2D):
            gqcnn_grasp.grasp_type = GQCNNGrasp.PARALLEL_JAW
        elif isinstance(grasp.grasp, SuctionPoint2D):
            gqcnn_grasp.grasp_type = GQCNNGrasp.SUCTION
        else:
            rospy.logerr('Grasp type not supported!')
            raise rospy.ServiceException('Grasp type not supported!')

        # store grasp representation in image space
        gqcnn_grasp.center_px[0] = grasp.grasp.center[0]
        gqcnn_grasp.center_px[1] = grasp.grasp.center[1]
        gqcnn_grasp.angle = grasp.grasp.angle
        gqcnn_grasp.depth = grasp.grasp.depth
        gqcnn_grasp.thumbnail = grasp.image.rosmsg
        
        # create and publish the pose alone for visualization ease of grasp pose in rviz
        pose_stamped = PoseStamped()
        pose_stamped.pose = grasp.grasp.pose().pose_msg
        header = Header()
        header.stamp = rospy.Time.now()
        header.frame_id = pose_frame
        pose_stamped.header = header
        grasp_pose_publisher.publish(pose_stamped)

        # return GQCNNGrasp msg
        rospy.loginfo('Total grasp planning time: ' + str(time.time() - grasp_planning_start_time) + ' secs.')

        return gqcnn_grasp

if __name__ == '__main__':
    
    # initialize the ROS node
    rospy.init_node('Grasp_Sampler_Server')

    # initialize cv_bridge
    cv_bridge = CvBridge()

    # get configs
    model_name = rospy.get_param('~model_name')
    model_dir = rospy.get_param('~model_dir')
    if model_dir.lower() == 'default':
        model_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                 '../models')
    model_dir = os.path.join(model_dir, model_name)
    model_config = json.load(open(os.path.join(model_dir, 'config.json'), 'r'))
    try:
        gqcnn_config = model_config['gqcnn']
        gripper_mode = gqcnn_config['gripper_mode']
    except:
        gqcnn_config = model_config['gqcnn_config']
        input_data_mode = gqcnn_config['input_data_mode']
        if input_data_mode == 'tf_image':
            gripper_mode = GripperMode.LEGACY_PARALLEL_JAW
        elif input_data_mode == 'tf_image_suction':
            gripper_mode = GripperMode.LEGACY_SUCTION                
        elif input_data_mode == 'suction':
            gripper_mode = GripperMode.SUCTION                
        elif input_data_mode == 'multi_suction':
            gripper_mode = GripperMode.MULTI_SUCTION                
        elif input_data_mode == 'parallel_jaw':
            gripper_mode = GripperMode.PARALLEL_JAW
        else:
            raise ValueError('Input data mode {} not supported!'.format(input_data_mode))

    # set config
    config_filename = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                   '..',
                                   'cfg/examples/ros/gqcnn_suction.yaml')
    if gripper_mode == GripperMode.LEGACY_PARALLEL_JAW or gripper_mode == GripperMode.PARALLEL_JAW:
        config_filename = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                       '..',
                                       'cfg/examples/ros/gqcnn_pj.yaml')

    # read config
    cfg = YamlConfig(config_filename)
    policy_cfg = cfg['policy']
    policy_cfg['metric']['gqcnn_model'] = model_dir

    # create publisher to publish pose only of final grasp
    grasp_pose_publisher = rospy.Publisher('/gqcnn_grasp/pose', PoseStamped, queue_size=10)

    # create a policy 
    rospy.loginfo('Creating Grasp Policy')
    grasping_policy = CrossEntropyRobustGraspingPolicy(policy_cfg)

    # create a grasp planner object
    grasp_planner = GraspPlanner(cfg, cv_bridge, grasping_policy, grasp_pose_publisher)

    # initialize the service        
    grasp_planning_service = rospy.Service('grasp_planner', GQCNNGraspPlanner, grasp_planner.plan_grasp)
    grasp_planning_service_bb = rospy.Service('grasp_planner_bounding_box', GQCNNGraspPlannerBoundingBox, grasp_planner.plan_grasp_bb)
    grasp_planning_service_segmask = rospy.Service('grasp_planner_segmask', GQCNNGraspPlannerSegmask, grasp_planner.plan_grasp_segmask)
    rospy.loginfo('Grasping Policy Initialized')

    # spin
    rospy.spin()