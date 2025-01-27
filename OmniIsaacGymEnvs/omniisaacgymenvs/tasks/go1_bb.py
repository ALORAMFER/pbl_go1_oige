# Copyright (c) 2018-2022, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from omniisaacgymenvs.tasks.base.rl_task import RLTask
from omniisaacgymenvs.robots.articulations.go1 import Go1
from omniisaacgymenvs.robots.articulations.bb import BB
from omniisaacgymenvs.robots.articulations.views.go1_view import Go1View
from omniisaacgymenvs.robots.articulations.views.bb_view import BBView
from omniisaacgymenvs.tasks.utils.usd_utils import set_drive

from omni.isaac.core.utils.prims import get_prim_at_path

from omni.isaac.core.utils.stage import add_reference_to_stage, get_current_stage

from omni.isaac.core.utils.torch.rotations import *

from omni.isaac.sensor import Camera
import omni.isaac.core.utils.numpy.rotations as rot_utils
import quaternion

import omni.usd
import numpy as np
import torch
import math
import matplotlib.pyplot as plt # to show camera view
from matplotlib.animation import ArtistAnimation

from pxr import Usd, UsdGeom, Sdf

from omni.isaac.core.prims import RigidPrimView

# Go1/A1にカメラを設置するためのモジュール群
# from omni.isaac.quadruped.robots.unitree_vision import UnitreeVision
from omniisaacgymenvs.exts.unitree_vision2 import UnitreeVision2
import omni.graph.core as og
from omni.isaac.core.utils.extensions import enable_extension 
# enable ROS bridge extension
enable_extension("omni.isaac.ros_bridge")


class Go1BBTask(RLTask):
    def __init__(
        self,
        name,
        sim_config,
        env,
        offset=None
    ) -> None:
        self._sim_config = sim_config
        self._cfg = sim_config.config
        self._task_cfg = sim_config.task_config

        # normalization
        self.lin_vel_scale = self._task_cfg["env"]["learn"]["linearVelocityScale"]
        self.ang_vel_scale = self._task_cfg["env"]["learn"]["angularVelocityScale"]
        self.dof_pos_scale = self._task_cfg["env"]["learn"]["dofPositionScale"]
        self.dof_vel_scale = self._task_cfg["env"]["learn"]["dofVelocityScale"]
        self.action_scale = self._task_cfg["env"]["control"]["actionScale"]

        # reward scales
        self.rew_scales = {}
        self.rew_scales["lin_vel_xy"] = self._task_cfg["env"]["learn"]["linearVelocityXYRewardScale"]
        self.rew_scales["ang_vel_z"] = self._task_cfg["env"]["learn"]["angularVelocityZRewardScale"]
        self.rew_scales["lin_vel_z"] = self._task_cfg["env"]["learn"]["linearVelocityZRewardScale"]
        self.rew_scales["joint_acc"] = self._task_cfg["env"]["learn"]["jointAccRewardScale"]
        self.rew_scales["action_rate"] = self._task_cfg["env"]["learn"]["actionRateRewardScale"]
        self.rew_scales["cosmetic"] = self._task_cfg["env"]["learn"]["cosmeticRewardScale"]
        self.rew_scales["body_cosmetic"] = self._task_cfg["env"]["learn"]["bodyCosmeticRewardScale"]
        self.min_body_height = self._task_cfg["env"]["learn"]["min_body_height"]

        # command ranges
        self.command_x_range = self._task_cfg["env"]["randomCommandVelocityRanges"]["linear_x"]
        self.command_yaw_range = self._task_cfg["env"]["randomCommandVelocityRanges"]["yaw"]

        # base init state
        pos = self._task_cfg["env"]["baseInitState"]["pos"]
        rot = self._task_cfg["env"]["baseInitState"]["rot"]
        v_lin = self._task_cfg["env"]["baseInitState"]["vLinear"]
        v_ang = self._task_cfg["env"]["baseInitState"]["vAngular"]
        state = pos + rot + v_lin + v_ang

        self.base_init_state = state

        # default joint positions
        self.named_default_joint_angles = self._task_cfg["env"]["defaultJointAngles"]

        # other
        self.dt = self._task_cfg["sim"]["dt"]
        self.max_episode_length_s = self._task_cfg["env"]["learn"]["episodeLength_s"]
        self.max_episode_length = int(self.max_episode_length_s / self.dt + 0.5)
        self.Kp = self._task_cfg["env"]["control"]["stiffness"]
        self.Kd = self._task_cfg["env"]["control"]["damping"]

        for key in self.rew_scales.keys():
            self.rew_scales[key] *= self.dt

        self._num_envs = self._task_cfg["env"]["numEnvs"]
        self._go1_translation = torch.tensor([0.0, 0.0, 0.4])
        self._bb_translation = torch.tensor([0.0, 0.0, 1.0]) # adding bb transration
        self._env_spacing = self._task_cfg["env"]["envSpacing"]
        self._num_observations = 44
        self._num_actions = 12

        RLTask.__init__(self, name, env)
        return

    def set_up_scene(self, scene) -> None:
        
        self.get_go1()
        self.get_bb() # adding bb
        super().set_up_scene(scene)
        self._go1s = Go1View(prim_paths_expr="/World/envs/.*/go1", name="go1view")
        self._bbs = BBView(prim_paths_expr="/World/envs/.*/bb", name="bbview") # setting bb view
    
        for i in range(self._num_envs):
            self.camera_0 = UnitreeVision2(
                                            prim_path="/World/envs/env_" + str(i) + "/go1/camera_optical_chin", 
                                            # prim_path="/World/envs/env_0/go1/camera_optical_chin", 
                                            index=i,
                                            name="Go1", 
                                            position=np.array([0, 0, 0.27]), 
                                            orientation = rot_utils.euler_angles_to_quats(np.array([90, 0, 90]), degrees=True),
                                            physics_dt=1 / 400.0, 
                                            model="Go1",
                                            camera_position= np.array([.0, .0, -0.9]),
                                            camera_degree=(180, 0, -180),  
                                        )

        scene.add(self._go1s)
        scene.add(self._go1s._base)
        scene.add(self._go1s._knees)
        scene.add(self._bbs) # adding
        # scene.add(self._bbs._base) # adding
        # scene.add(self._bbs._knees) # adding
        
        return

    def get_go1(self):
        go1 = Go1(prim_path=self.default_zero_env_path + "/go1", name="Go1", translation=self._go1_translation, usd_path="/isaac-sim/OmniIsaacGymEnvs/omniisaacgymenvs/USD/go1.usd")
        self._sim_config.apply_articulation_settings("Go1", get_prim_at_path(go1.prim_path), self._sim_config.parse_actor_config("Go1"))        
  
        # Configure joint properties
        #for quadrant in ["FL", "RL", "FR", "RR"]:
        #    set_drive(f"{go1.prim_path}/trunk/{quadrant}_hip_joint", "angular", "position", 0, self.Kp, self.Kd, 23.7)
        #    set_drive(f"{go1.prim_path}/{quadrant}_hip/{quadrant}_thigh_joint", "angular", "position", 0, self.Kp, self.Kd, 23.7)
        #    set_drive(f"{go1.prim_path}/{quadrant}_thigh/{quadrant}_calf_joint", "angular", "position", 0, self.Kp, self.Kd, 35.55)

        RigidPrimView(prim_paths_expr="/World/envs/.*/go1/.*_calf", name="knees_view", reset_xform_properties=False)

        self.default_dof_pos = torch.zeros((self.num_envs, 12), dtype=torch.float, device=self.device, requires_grad=False)
        dof_names = go1.dof_names
        for i in range(self.num_actions):
            name = dof_names[i]
            angle = self.named_default_joint_angles[name]
            self.default_dof_pos[:, i] = angle

    def get_bb(self): 
        bb = BB(prim_path=self.default_zero_env_path + "/bb", 
                name="BB", 
                translation=self._bb_translation, 
                usd_path="/isaac-sim/OmniIsaacGymEnvs/omniisaacgymenvs/USD/bb2.usd") # mazuha go1 usd
        # self._sim_config.apply_articulation_settings("BB", get_prim_at_path(bb.prim_path), self._sim_config.parse_actor_config("BB"))

        # Configure joint properties
        #for quadrant in ["FL", "RL", "FR", "RR"]:
        #    set_drive(f"{go1.prim_path}/trunk/{quadrant}_hip_joint", "angular", "position", 0, self.Kp, self.Kd, 23.7)
        #    set_drive(f"{go1.prim_path}/{quadrant}_hip/{quadrant}_thigh_joint", "angular", "position", 0, self.Kp, self.Kd, 23.7)
        #    set_drive(f"{go1.prim_path}/{quadrant}_thigh/{quadrant}_calf_joint", "angular", "position", 0, self.Kp, self.Kd, 35.55)

        # RigidPrimView(prim_paths_expr="/World/envs/.*/bb/.*_calf", name="knees_view", reset_xform_properties=False)
        # RigidPrimView(prim_paths_expr="/World/envs/.*/bb", name="bb_view", reset_xform_properties=False)

        # self.default_dof_pos = torch.zeros((self.num_envs, 12), dtype=torch.float, device=self.device, requires_grad=False)
        # dof_names = bb.dof_names
        # for i in range(self.num_actions):
        #     name = dof_names[i]
        #     angle = self.named_default_joint_angles[name]
        #     self.default_dof_pos[:, i] = angle
    


    def get_observations(self) -> dict:
        torso_position, torso_rotation = self._go1s.get_world_poses(clone=False)
        root_velocities = self._go1s.get_velocities(clone=False)
        dof_pos = self._go1s.get_joint_positions(clone=False)
        dof_vel = self._go1s.get_joint_velocities(clone=False)

        #velocity = root_velocities[:, 0:3]
        ang_velocity = root_velocities[:, 3:6]

        #base_lin_vel = quat_rotate_inverse(torso_rotation, velocity) * self.lin_vel_scale
        base_ang_vel = quat_rotate_inverse(torso_rotation, ang_velocity) * self.ang_vel_scale
        projected_gravity = quat_rotate(torso_rotation, self.gravity_vec)
        dof_pos_scaled = (dof_pos - self.default_dof_pos) * self.dof_pos_scale

        commands_scaled = self.commands * torch.tensor(
            [self.lin_vel_scale, self.ang_vel_scale],
            requires_grad=False,
            device=self.commands.device,
        )

        obs = torch.cat(
            (
                #base_lin_vel,
                base_ang_vel,
                projected_gravity,
                commands_scaled,
                dof_pos_scaled,
                dof_vel * self.dof_vel_scale,
                self.actions,
            ),
            dim=-1,
        )
        self.obs_buf[:] = obs

        observations = {
            self._go1s.name: {
                "obs_buf": self.obs_buf
            }
        }
        return observations

    def pre_physics_step(self, actions) -> None:
        reset_env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        if len(reset_env_ids) > 0:
            self.reset_idx(reset_env_ids)

        indices = torch.arange(self._go1s.count, dtype=torch.int32, device=self._device)
        self.actions[:] = actions.clone().to(self._device)
        
        current_targets = self.current_targets + self.action_scale * self.actions
        self.current_targets[:] = torch.clamp(current_targets, self.go1_dof_lower_limits, self.go1_dof_upper_limits)
        self._go1s.set_joint_position_targets(self.current_targets, indices)

    def reset_idx(self, env_ids):
        num_resets = len(env_ids)
        # randomize DOF velocities
        velocities = torch_rand_float(-0.1, 0.1, (num_resets, self._go1s.num_dof), device=self._device)
        dof_pos = self.default_dof_pos[env_ids]
        dof_vel = velocities

        self.current_targets[env_ids] = dof_pos[:]

        root_vel = torch.zeros((num_resets, 6), device=self._device)

        # apply resets
        indices = env_ids.to(dtype=torch.int32)
        self._go1s.set_joint_positions(dof_pos, indices)
        self._go1s.set_joint_velocities(dof_vel, indices)

        self._go1s.set_world_poses(self.initial_root_pos[env_ids].clone(), self.initial_root_rot[env_ids].clone(), indices)
        self._go1s.set_velocities(root_vel, indices)

        self.commands_x[env_ids] = torch_rand_float(
            self.command_x_range[0], self.command_x_range[1], (num_resets, 1), device=self._device
        ).squeeze()

        self.commands_yaw[env_ids] = torch_rand_float(
            self.command_yaw_range[0], self.command_yaw_range[1], (num_resets, 1), device=self._device
        ).squeeze()

        # bookkeeping
        self.reset_buf[env_ids] = 0
        self.progress_buf[env_ids] = 0
        self.last_actions[env_ids] = 0.
        self.last_dof_vel[env_ids] = 0.

    def post_reset(self):
        self.initial_root_pos, self.initial_root_rot = self._go1s.get_world_poses()
        self.current_targets = self.default_dof_pos.clone()

        dof_limits = self._go1s.get_dof_limits()
        self.go1_dof_lower_limits = dof_limits[0, :, 0].to(device=self._device)
        self.go1_dof_upper_limits = dof_limits[0, :, 1].to(device=self._device)

        self.commands = torch.zeros(self._num_envs, 2, dtype=torch.float, device=self._device, requires_grad=False)
        self.commands_x = self.commands.view(self._num_envs, 2)[..., 0]
        self.commands_yaw = self.commands.view(self._num_envs, 2)[..., 1]

        # initialize some data used later on
        self.extras = {}
        self.gravity_vec = torch.tensor([0.0, 0.0, -1.0], device=self._device).repeat(
            (self._num_envs, 1)
        )
        self.actions = torch.zeros(
            self._num_envs, self.num_actions, dtype=torch.float, device=self._device, requires_grad=False
        )
        self.last_dof_vel = torch.zeros((self._num_envs, 12), dtype=torch.float, device=self._device, requires_grad=False)
        self.last_actions = torch.zeros(self._num_envs, self.num_actions, dtype=torch.float, device=self._device, requires_grad=False)

        self.time_out_buf = torch.zeros_like(self.reset_buf)

        # randomize all envs
        indices = torch.arange(self._go1s.count, dtype=torch.int64, device=self._device)
        self.reset_idx(indices)

        stage = omni.usd.get_context().get_stage()
        ground_prim = stage.GetPrimAtPath("/World/defaultGroundPlane/GroundPlane/CollisionPlane")
        ground_prim.GetAttribute("physics:collisionEnabled").Set(False)
        ground_prim.GetAttribute("physics:collisionEnabled").Set(True)
        
        # print(self.camera.cameras)

    def calculate_metrics(self) -> None:
        torso_position, torso_rotation = self._go1s.get_world_poses(clone=False)
        root_velocities = self._go1s.get_velocities(clone=False)
        dof_pos = self._go1s.get_joint_positions(clone=False)
        dof_vel = self._go1s.get_joint_velocities(clone=False)

        velocity = root_velocities[:, 0:3]
        ang_velocity = root_velocities[:, 3:6]

        base_lin_vel = quat_rotate_inverse(torso_rotation, velocity)
        base_ang_vel = quat_rotate_inverse(torso_rotation, ang_velocity)

        # velocity tracking reward
        lin_vel_error = torch.square(self.commands[:, 0] - base_lin_vel[:, 0])
        ang_vel_error = torch.square(self.commands[:, 1] - base_ang_vel[:, 2])
        rew_lin_vel_xy = torch.exp(-lin_vel_error / 0.25) * self.rew_scales["lin_vel_xy"]
        rew_ang_vel_z = torch.exp(-ang_vel_error / 0.25) * self.rew_scales["ang_vel_z"]

        rew_lin_vel_z = torch.square(base_lin_vel[:, 2]) * self.rew_scales["lin_vel_z"]
        rew_joint_acc = torch.sum(torch.square(self.last_dof_vel - dof_vel), dim=1) * self.rew_scales["joint_acc"]
        rew_action_rate = torch.sum(torch.square(self.last_actions - self.actions), dim=1) * self.rew_scales["action_rate"]
        rew_cosmetic = torch.sum(torch.abs(dof_pos[:, 0:4] - self.default_dof_pos[:, 0:4]), dim=1) * self.rew_scales["cosmetic"]
        rew_body_cosmetic = torch.sum(torch.abs(torso_rotation[:, 1:3]), dim=1) * self.rew_scales["body_cosmetic"]

        total_reward = rew_lin_vel_z + rew_lin_vel_xy + rew_ang_vel_z + rew_joint_acc  + rew_action_rate + rew_cosmetic + rew_body_cosmetic
        total_reward = torch.clip(total_reward, 0.0, None)

        self.last_actions[:] = self.actions[:]
        self.last_dof_vel[:] = dof_vel[:]

        self.fallen_over = self._go1s.is_base_below_threshold(threshold=self.min_body_height, ground_heights=0.0)

        total_reward[torch.nonzero(self.fallen_over)] = -1
        self.rew_buf[:] = total_reward.detach()

    def is_done(self) -> None:
        # reset agents
        time_out = self.progress_buf >= self.max_episode_length - 1
        self.reset_buf[:] = time_out | self.fallen_over

