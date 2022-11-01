"""
A collection of utils to easily command drones with AeroStack2.
"""

# Copyright (c) 2022 Universidad Politécnica de Madrid
# All Rights Reserved
#
# Licensed under the BSD-3-Clause (the "License");
# you may not use this file except in compliance with the License.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

__authors__ = "Miguel Fernández Cortizas, Pedro Arias Pérez, David Pérez Saura, Rafael Pérez Seguí"
__copyright__ = "Copyright (c) 2022 Universidad Politécnica de Madrid"
__license__ = "BSD-3-Clause"
__version__ = "0.1.0"

import threading
from time import sleep
from typing import List, Dict, Union

import rclpy
import rclpy.signals
import rclpy.executors
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data, qos_profile_system_default
from rclpy.parameter import Parameter
import message_filters

from sensor_msgs.msg import NavSatFix
from as2_msgs.msg import TrajectoryWaypoints, PlatformInfo, ControlMode
from as2_msgs.srv import SetOrigin, GeopathToPath, PathToGeopath, SetControlMode
from geometry_msgs.msg import Pose, PoseStamped, TwistStamped
from geographic_msgs.msg import GeoPose
from nav_msgs.msg import Path

from .shared_data.platform_info_data import PlatformInfoData
from .shared_data.pose_data import PoseData
from .shared_data.gps_data import GpsData

from .behaviour_actions.gotowayp_behaviour import SendGoToWaypoint
from .behaviour_actions.takeoff_behaviour import SendTakeoff
from .behaviour_actions.followpath_behaviour import SendFollowPath
from .behaviour_actions.land_behaviour import SendLand

from .service_clients.arming import Arm, Disarm
from .service_clients.offboard import Offboard

from .tools.utils import euler_from_quaternion


STATE = ["DISARMED", "LANDED", "TAKING_OFF", "FLYING", "LANDING", "EMERGENCY"]
YAW_MODE = ["NONE", "YAW_ANGLE", "YAW_SPEED"]
CONTROL_MODE = ["UNSET", "HOVER", "POSITION", "SPEED", "SPEED_IN_A_PLANE",
                "ATTITUDE", "ACRO", "TRAJECTORY", "ACEL"]
REFERENCE_FRAME = ["UNDEFINED_FRAME", "LOCAL_ENU_FRAME", "BODY_FLU_FRAME", "GLOBAL_ENU_FRAME"]


class DroneInterface(Node):
    """Drone interface node"""
    def __init__(self, drone_id: str = "drone0", verbose: bool = False,
                 use_gps: bool = False, use_sim_time: bool = False) -> None:
        super().__init__(f'{drone_id}_interface', namespace=drone_id)

        self.param_use_sim_time = Parameter('use_sim_time', Parameter.Type.BOOL, use_sim_time)
        self.set_parameters([self.param_use_sim_time])

        self.__executor = rclpy.executors.SingleThreadedExecutor()
        if verbose:
            self.get_logger().set_level(rclpy.logging.LoggingSeverity.DEBUG)

        self.info = PlatformInfoData()
        self.pose = PoseData()

        self.namespace = drone_id
        print(f"Starting {self.drone_id}")

        self.info_sub = self.create_subscription(
            PlatformInfo, 'platform/info', self.info_callback, qos_profile_system_default)

        # TODO: Synchronious callbacks to pose and twist
        # self.pose_sub = message_filters.Subscriber(self, PoseStamped,
        #   'self_localization/pose', qos_profile_sensor_data.get_c_qos_profile())
        # self.twist_sub = message_filters.Subscriber(self, TwistStamped,
        #   'self_localization/twist', qos_profile_sensor_data.get_c_qos_profile())

        # self._synchronizer = message_filters.ApproximateTimeSynchronizer(
        #     (self.pose_sub, self.twist_sub), 5, 0.01, allow_headerless=True)
        # self._synchronizer.registerCallback(self.pose_callback)

        # Pose subscriber
        self.pose_sub = self.create_subscription(
            PoseStamped, 'self_localization/pose', self.pose_callback, qos_profile_sensor_data)

        self.gps_sub = self.create_subscription(
            NavSatFix, 'sensor_measurements/gps', self.gps_callback, qos_profile_sensor_data)

        translator_namespace = self.namespace
        self.global_to_local_cli_ = self.create_client(
            GeopathToPath, f"{translator_namespace}/geopath_to_path")
        self.local_to_global_cli_ = self.create_client(
            PathToGeopath, f"{translator_namespace}/path_to_geopath")

        self.use_gps = use_gps
        self.gps = GpsData()
        if self.use_gps:
            self.set_origin_cli_ = self.create_client(
                SetOrigin, f"{translator_namespace}/set_origin")
            if not self.set_origin_cli_.wait_for_service(timeout_sec=3):
                self.get_logger().warn("Set Origin not ready")

        self.set_control_mode_cli_ = self.create_client(
            SetControlMode, 'controller/set_control_mode')

        if not self.set_control_mode_cli_.wait_for_service(timeout_sec=3):
            self.get_logger().error("Set control mode not available")

        self.motion_reference_pose_pub_ = self.create_publisher(
            PoseStamped,  'motion_reference/pose',  qos_profile_sensor_data)
        self.motion_reference_twist_pub_ = self.create_publisher(
            TwistStamped, 'motion_reference/twist', qos_profile_sensor_data)

        self.control_mode_ = ControlMode()

        # self.__executor.add_node(self)
        # self.__executor.spin()
        # self.__executor.shutdown()
        # rclpy.shutdown()

        self.keep_running = True
        self.__executor.add_node(self)
        self.spin_thread = threading.Thread(target=self.auto_spin)
        self.spin_thread.start()

        sleep(0.5)
        self.get_logger().info(f'{self.drone_id} interface initialized')

    def __del__(self) -> None:
        self.shutdown()

    @property
    def drone_id(self) -> str:
        """drone id / namespace getter"""
        return self.namespace

    def info_callback(self, msg: PlatformInfo) -> None:
        """platform info callback"""
        self.info.data = [int(msg.connected), int(msg.armed), int(msg.offboard), msg.status.state,
                          msg.current_control_mode.yaw_mode, msg.current_control_mode.control_mode,
                          msg.current_control_mode.reference_frame]

    def __get_info(self) -> List[int]:
        return self.info.data

    @property
    def info(self) -> Dict[str, Union[bool, str]]:
        """get drone info"""
        info = self.__get_info()
        return {"connected": bool(info[0]), "armed": bool(info[1]), "offboard": bool(info[2]),
                "state": STATE[info[3]],"yaw_mode": YAW_MODE[info[4]],
                "control_mode": CONTROL_MODE[info[5]], "reference_frame": REFERENCE_FRAME[info[6]]}

    def pose_callback(self, pose_msg: PoseStamped) -> None:
        """pose stamped callback"""
        self.pose.position = [pose_msg.pose.position.x,
                              pose_msg.pose.position.y,
                              pose_msg.pose.position.z]

        self.pose.orientation = [
            *euler_from_quaternion(
                pose_msg.pose.orientation.x,
                pose_msg.pose.orientation.y,
                pose_msg.pose.orientation.z,
                pose_msg.pose.orientation.w)]

    @property
    def position(self) -> List[float]:
        """drone position getter"""
        return self.pose.position

    @property
    def orientation(self) -> List[float]:
        """drone orientation getter"""
        return self.pose.orientation

    def gps_callback(self, msg: NavSatFix) -> None:
        """navdata (gps) callback"""
        self.gps.fix = [msg.latitude, msg.longitude, msg.altitude]

    @property
    def gps_pose(self) -> List[float]:
        """gps pose getter"""
        return self.gps.fix

    def set_home(self, gps_pose_: List[float]) -> None:
        """Set home origin"""
        if not self.set_origin_cli_.wait_for_service(timeout_sec=3):
            self.get_logger().error("GPS service not available")
            return

        req = SetOrigin.Request()
        req.origin.latitude = float(gps_pose_[0])
        req.origin.longitude = float(gps_pose_[1])
        req.origin.altitude = float(gps_pose_[2])
        resp = self.set_origin_cli_.call(req)
        if not resp.success:
            self.get_logger().warn("Origin already set")

    def __follow_path(self, path: Path, speed: float, yaw_mode: int, is_gps: bool = False) -> None:
        path_data = SendFollowPath.FollowPathData(
            path, speed, yaw_mode, is_gps)
        SendFollowPath(self, path_data)

    def takeoff(self, height: float = 1.0, speed: float = 0.5) -> None:
        """Drone takeoff"""
        if self.use_gps:
            self.set_home(self.gps_pose)

        SendTakeoff(self, float(height), float(speed))

    def follow_path(self, path: Path, speed: float = 1.0,
                    yaw_mode: int = TrajectoryWaypoints.KEEP_YAW) -> None:
        """Drone follow path"""
        self.__follow_path(path, speed, yaw_mode)

    def follow_gps_path(self, wp_path: Path, speed: float = 1.0,
                        yaw_mode: int = TrajectoryWaypoints.KEEP_YAW) -> None:
        """Drone follow gps path"""
        self.__follow_path(wp_path, speed, yaw_mode, is_gps=True)

    def arm(self) -> None:
        """Drone arming"""
        sleep(0.1)
        Arm(self)

    def disarm(self) -> None:
        """Drone disarming"""
        Disarm(self)

    def offboard(self) -> None:
        """Drone set offboard"""
        Offboard(self)

    def land(self, speed: float = 0.5) -> None:
        """Drone landing"""
        SendLand(self, float(speed))

    def __go_to(self, _x: float, _y: float, _z: float,
                speed: float, ignore_yaw: bool, is_gps: bool) -> None:
        if is_gps:
            msg = GeoPose()
            msg.position.latitude = (float)(_x)
            msg.position.longitude = (float)(_y)
            msg.position.altitude = (float)(_z)
        else:
            msg = Pose()
            msg.position.x = (float)(_x)
            msg.position.y = (float)(_y)
            msg.position.z = (float)(_z)
        SendGoToWaypoint(self, msg, speed, ignore_yaw)

    def go_to(self, _x: float, _y: float, _z: float, speed: float, ignore_yaw: bool = True) -> None:
        """Drone go to"""
        self.__go_to(_x, _y, _z, speed, ignore_yaw, is_gps=False)

    # TODO: python overloads?
    def go_to_point(self, point: List[float, float, float],
                    speed: float, ignore_yaw: bool = True) -> None:
        """Drone go to"""
        self.__go_to(point[0], point[1], point[2], speed, ignore_yaw, is_gps=False)

    def go_to_gps(self, lat: float, lon: float, alt: float,
                  speed: float, ignore_yaw: bool = True) -> None:
        """Drone go to gps pose"""
        self.__go_to(lat, lon, alt, speed, ignore_yaw, is_gps=True)

    # TODO: python overloads?
    def go_to_gps_point(self, waypoint: List[float, float, float],
                        speed: float, ignore_yaw: bool = True) -> None:
        """Drone go to gps point"""
        self.__go_to(waypoint[0], waypoint[1], waypoint[2], speed, ignore_yaw, is_gps=True)

    def set_mode(self, mode: ControlMode) -> bool:
        """Set control mode"""
        if isinstance(mode) != ControlMode:
            print("Invalid mode")
            raise TypeError

        req = SetControlMode.Request()
        req.control_mode = mode
        resp = self.set_control_mode_cli_.call(req)
        if resp.success:
            self.control_mode_ = mode
            return True

        print("Failed to set mode")
        return False

    def send_motion_reference_pose(self, position: List[float],
                                   orientation: List[float] = [0.0, 0.0, 0.0, 1.0]) -> None:
        """Send motion reference pose"""
        desired_control_mode_ = ControlMode()
        desired_control_mode_.control_mode = ControlMode.POSITION
        desired_control_mode_.yaw_mode = ControlMode.YAW_ANGLE
        desired_control_mode_.reference_frame = ControlMode.LOCAL_ENU_FRAME

        if (self.control_mode_.control_mode != desired_control_mode_.control_mode or
            self.control_mode_.yaw_mode != desired_control_mode_.yaw_mode or
                self.control_mode_.reference_frame != desired_control_mode_.reference_frame):
            success = self.set_mode(desired_control_mode_)

            if not success:
                return

        send_pose = PoseStamped()
        send_pose.header.frame_id = "earth"
        send_pose.pose.position.x = position[0]
        send_pose.pose.position.y = position[1]
        send_pose.pose.position.z = position[2]

        send_pose.pose.orientation.x = orientation[0]
        send_pose.pose.orientation.y = orientation[1]
        send_pose.pose.orientation.z = orientation[2]
        send_pose.pose.orientation.w = orientation[3]

        self.motion_reference_pose_pub_.publish(send_pose)

        send_twist = TwistStamped()
        send_twist.header.frame_id = self.drone_id + "/base_link"
        send_twist.twist.linear.x = 0.0
        send_twist.twist.linear.y = 0.0
        send_twist.twist.linear.z = 0.0

        send_twist.twist.angular.x = 0.0
        send_twist.twist.angular.y = 0.0
        send_twist.twist.angular.z = 0.0

        self.motion_reference_twist_pub_.publish(send_twist)

    def send_motion_reference_twist(self, lineal: List[float],
                                    angular: List[float] = [0.0, 0.0, 0.0]) -> None:
        """Send motion reference twist"""
        desired_control_mode_ = ControlMode()
        desired_control_mode_.control_mode = ControlMode.SPEED
        desired_control_mode_.yaw_mode = ControlMode.YAW_SPEED
        desired_control_mode_.reference_frame = ControlMode.LOCAL_ENU_FRAME

        if (self.control_mode_.control_mode != desired_control_mode_.control_mode or
            self.control_mode_.yaw_mode != desired_control_mode_.yaw_mode or
                self.control_mode_.reference_frame != desired_control_mode_.reference_frame):
            success = self.set_mode(desired_control_mode_)

            if not success:
                return

        send_twist = TwistStamped()
        send_twist.twist.linear.x = lineal[0]
        send_twist.twist.linear.y = lineal[1]
        send_twist.twist.linear.z = lineal[2]

        send_twist.twist.angular.x = angular[0]
        send_twist.twist.angular.y = angular[1]
        send_twist.twist.angular.z = angular[2]

        self.motion_reference_twist_pub_.publish(send_twist)

    # TODO: replace with executor callbacks
    def auto_spin(self) -> None:
        """Drone intern spin"""
        while rclpy.ok() and self.keep_running:
            self.__executor.spin_once()
            sleep(0.05)

    def shutdown(self) -> None:
        """Shutdown properly"""
        self.keep_running = False
        self.destroy_subscription(self.info_sub)
        self.destroy_subscription(self.pose_sub)
        self.destroy_subscription(self.gps_sub)

        if self.use_gps:
            self.destroy_client(self.set_origin_cli_)
        self.destroy_client(self.global_to_local_cli_)
        self.destroy_client(self.local_to_global_cli_)

        self.spin_thread.join()
        print("Clean exit")
