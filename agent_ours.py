# BSD 2-Clause License

# Copyright (c) 2023, Bandi Jai Krishna

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.

# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.

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

import sys
import time
import math
import numpy as np
import struct
import torch
import scipy
import threading

import rospy
from sensor_msgs.msg import JointState, Imu
from std_msgs.msg import Float32MultiArray
from std_msgs.msg import Header

from actor_critic import ActorCritic
sys.path.append('unitree_legged_sdk/lib/python/amd64')
import robot_interface as sdk

class Agent2():
    def __init__(self,path):
        self.dt = 0.02
        self.num_actions = 12
        self.num_obs = 42*5
        self.unit_obs = 42
        self.num_privl_obs = 42 * 5 + 6 + 187
        self.device = 'cpu'
        self.path = path#'bp4/model_1750.pt'
        self.d = {'FR_0':0, 'FR_1':1, 'FR_2':2,
                'FL_0':3, 'FL_1':4, 'FL_2':5, 
                'RR_0':6, 'RR_1':7, 'RR_2':8, 
                'RL_0':9, 'RL_1':10, 'RL_2':11 }
        PosStopF  = math.pow(10,9)
        VelStopF  = 16000.0
        HIGHLEVEL = 0xee
        LOWLEVEL  = 0xff
        self.init = True
        self.motiontime = 0 
        self.timestep = 0
        self.time = 0
        
#####################################################################
        self.euler = np.zeros(3)
        self.buf_idx = 0

        self.smoothing_length = 12
        self.deuler_history = np.zeros((self.smoothing_length, 3))
        self.dt_history = np.zeros((self.smoothing_length, 1))
        self.euler_prev = np.zeros(3)
        self.timuprev = time.time()

        self.body_ang_vel = np.zeros(3)
        self.smoothing_ratio = 0.2
#####################################################################

        self.default_angles = [0.1,0.8,-1.5,-0.1,0.8,-1.5,0.1,1,-1.5,-0.1,1,-1.5]
        self.default_angles_tensor = torch.tensor([0.1,0.8,-1.5,-0.1,0.8,-1.5,0.1,1,-1.5,-0.1,1,-1.5],device=self.device,dtype=torch.float,requires_grad=False)
        
        self.actions = torch.zeros(self.num_actions,device=self.device,dtype=torch.float,requires_grad=False)
        self.obs = torch.zeros(self.num_obs,device=self.device,dtype=torch.float,requires_grad=False)
        self.obs_storage = torch.zeros(self.unit_obs*4,device=self.device,dtype=torch.float)

        actor_critic = ActorCritic(num_actor_obs=self.num_obs,num_critic_obs=self.num_privl_obs,num_actions=12,actor_hidden_dims = [512, 256, 128],critic_hidden_dims = [512, 256, 128],activation = 'elu',init_noise_std = 1.0)
        loaded_dict = torch.load(self.path)
        actor_critic.load_state_dict(loaded_dict['model_state_dict'])
        actor_critic.eval()
        self.policy = actor_critic.act_inference

        self.udp = sdk.UDP(LOWLEVEL, 8080, "192.168.123.10", 8007)
        self.safe = sdk.Safety(sdk.LeggedType.Go1)

        self.cmd = sdk.LowCmd()
        self.state = sdk.LowState()
        self.udp.InitCmdData(self.cmd)

    def get_body_angular_vel(self):
        # self.body_ang_vel = self.smoothing_ratio * np.mean(self.deuler_history / self.dt_history, axis=0) + (
        #             1 - self.smoothing_ratio) * self.body_ang_vel

        self.body_ang_vel = self.smoothing_ratio * np.array(self.state.imu.gyroscope) + (1 - self.smoothing_ratio) * self.body_ang_vel

        return self.body_ang_vel
    
    def get_observations(self):
        self.euler = np.array(self.state.imu.rpy)
        self.deuler_history[self.buf_idx % self.smoothing_length, :] = self.euler  - self.euler_prev
        self.dt_history[self.buf_idx % self.smoothing_length] = time.time() - self.timuprev
        self.timuprev = time.time()
        self.buf_idx += 1
        self.euler_prev = self.euler

        lx = struct.unpack('f', struct.pack('4B', *self.state.wirelessRemote[4:8]))
        ly = struct.unpack('f', struct.pack('4B', *self.state.wirelessRemote[20:24]))
        rx = struct.unpack('f', struct.pack('4B', *self.state.wirelessRemote[8:12]))
        # ry = struct.unpack('f', struct.pack('4B', *self.state.wirelessRemote[12:16]))
        forward = ly[0]*0.6  
        if abs(forward) <0.10:
            forward = 0
        side = -lx[0]*0.5
        if abs(side) <0.1:
            side = 0
        rotate = -rx[0]*0.8
        if abs(rotate) <0.1:
            rotate = 0

        self.pitch = torch.tensor([self.state.imu.rpy[1]],device=self.device,dtype=torch.float,requires_grad=False)
        self.roll = torch.tensor([self.state.imu.rpy[0]],device=self.device,dtype=torch.float,requires_grad=False)
        angles = self.getJointPos()
        vel = self.getJointVelocity()

        self.dof_pos = torch.tensor([m - n for m,n in zip(angles,self.default_angles)],device=self.device,dtype=torch.float,requires_grad=False)
        # body_ang_vel = self.get_body_angular_vel() #self.state.imu.gyroscope  #[state.imu.gyroscope]
        body_ang_vel = self.state.imu.gyroscope
        # print(vel[1])
        if self.timestep > 1600:
            self.base_ang_vel = torch.tensor([body_ang_vel],device=self.device,dtype=torch.float,requires_grad=False)
            self.dof_vel = torch.tensor([vel],device=self.device,dtype=torch.float,requires_grad=False)
            # self.dof_pos = torch.tensor([angles], device=self.device, dtype=torch.float, requires_grad=False)
        else:
            self.base_ang_vel = 0*torch.tensor([body_ang_vel],device=self.device,dtype=torch.float,requires_grad=False)
            self.dof_vel = 0*torch.tensor([vel],device=self.device,dtype=torch.float,requires_grad=False)
            # self.dof_pos = self.default_angles_tensor
        if self.timestep > 2000:
            # self.commands = torch.tensor([0.5,0,0],device=self.device,dtype=torch.float,requires_grad=False)
            self.commands = torch.tensor([forward,side,rotate],device=self.device,dtype=torch.float,requires_grad=False)
        #     print(f"{vel[1]} | {self.base_ang_vel}")
        else:
            self.commands = torch.tensor([0,0,0],device=self.device,dtype=torch.float,requires_grad=False)

        self.obs = torch.cat((
            self.base_ang_vel.squeeze(),
            self.commands,
            self.dof_pos.squeeze(),
            self.dof_vel.squeeze(),
            self.actions,
            ),dim=-1)
        current_obs = self.obs.clone()
        # print("ang vel: ", self.base_ang_vel.squeeze())
        # print("commands: ", self.commands)
        # print("dof pos: ", self.dof_pos.squeeze())
        # print("dof vel: ", self.dof_vel.squeeze())
        # print("actions: ", self.actions)
        self.obs = torch.cat((self.obs,self.obs_storage),dim=-1)

        # for j in range(5):
        #     self.obs[self.unit_obs*j+3] = forward
        #     self.obs[self.unit_obs*j+4] = side
        #     self.obs[self.unit_obs*j+5] = rotate

        self.obs_storage[:-self.unit_obs] = self.obs_storage[self.unit_obs:].clone()
        self.obs_storage[-self.unit_obs:] = current_obs

    def init_pose(self):
        while self.init:
            self.pre_step()
            self.get_observations()
            self.motiontime = self.motiontime+1
            if self.motiontime <100:
                self.setJointValues(self.default_angles,kp=5,kd=1)
            else:
                self.setJointValues(self.default_angles,kp=50,kd=5)
                # self.setJointValues(self.default_angles,kp=20,kd=0.5)
            if self.motiontime > 1100:
                self.init = False
            self.post_step()
        print("Starting")
        while True:
            self.step()

    def pre_step(self):
        self.udp.Recv()
        self.udp.GetRecv(self.state)
    
    def step(self):
        '''
        Has to be called after init_pose 
        calls pre_step for getting udp packets
        calls policy with obs, clips and scales actions and adds default pose before sending them to robot
        calls post_step 
        '''
        self.pre_step()
        self.get_observations()
        self.actions = self.policy(self.obs)
        actions = torch.clip(self.actions, -100, 100).to('cpu').detach()
        scaled_actions = actions * 0.25
        # print("final angle: ", scaled_actions+self.default_angles_tensor)
        final_angles = scaled_actions+self.default_angles_tensor
        # final_angles = self.default_angles_tensor

        # print("actions: ", final_angles)
        # print("actions:" + ",".join(map(str, actions.numpy().tolist())))
        # print("observations:" + str(time.process_time()) + ",".join(map(str, self.obs.detach().numpy().tolist())))

        self.setJointValues(angles=final_angles,kp=20,kd=0.5)
        self.post_step()

    def post_step(self):
        '''
        Offers power protection, sends udp packets, maintains timing
        '''
        self.safe.PowerProtect(self.cmd, self.state, 9)
        self.udp.SetSend(self.cmd)
        self.udp.Send()
        time.sleep(max(self.dt - (time.time() - self.time), 0))
        if self.timestep % 100 == 0: 
            print(f"{self.timestep}| frq: {1 / (time.time() - self.time)} Hz")
        self.time = time.time()
        self.timestep = self.timestep + 1

    def getJointVelocity(self):
        velocity = [self.state.motorState[self.d['FL_0']].dq,self.state.motorState[self.d['FL_1']].dq,self.state.motorState[self.d['FL_2']].dq,
                self.state.motorState[self.d['FR_0']].dq,self.state.motorState[self.d['FR_1']].dq,self.state.motorState[self.d['FR_2']].dq,
                self.state.motorState[self.d['RL_0']].dq,self.state.motorState[self.d['RL_1']].dq,self.state.motorState[self.d['RL_2']].dq,
                self.state.motorState[self.d['RR_0']].dq,self.state.motorState[self.d['RR_1']].dq,self.state.motorState[self.d['RR_2']].dq]
        return velocity
    
    def getJointTorque(self):
        torque = [self.state.motorState[self.d['FL_0']].tauEst,self.state.motorState[self.d['FL_1']].tauEst,self.state.motorState[self.d['FL_2']].tauEst,
                self.state.motorState[self.d['FR_0']].tauEst,self.state.motorState[self.d['FR_1']].tauEst,self.state.motorState[self.d['FR_2']].tauEst,
                self.state.motorState[self.d['RL_0']].tauEst,self.state.motorState[self.d['RL_1']].tauEst,self.state.motorState[self.d['RL_2']].tauEst,
                self.state.motorState[self.d['RR_0']].tauEst,self.state.motorState[self.d['RR_1']].tauEst,self.state.motorState[self.d['RR_2']].tauEst]
        return torque
    
    def getJointPos(self):
        current_angles = [
        self.state.motorState[self.d['FL_0']].q,self.state.motorState[self.d['FL_1']].q,self.state.motorState[self.d['FL_2']].q,
        self.state.motorState[self.d['FR_0']].q,self.state.motorState[self.d['FR_1']].q,self.state.motorState[self.d['FR_2']].q,
        self.state.motorState[self.d['RL_0']].q,self.state.motorState[self.d['RL_1']].q,self.state.motorState[self.d['RL_2']].q,
        self.state.motorState[self.d['RR_0']].q,self.state.motorState[self.d['RR_1']].q,self.state.motorState[self.d['RR_2']].q]
        return current_angles
    
    def setJointValues(self,angles,kp,kd):
        self.cmd.motorCmd[self.d['FR_0']].q = angles[3]
        self.cmd.motorCmd[self.d['FR_0']].dq = 0
        self.cmd.motorCmd[self.d['FR_0']].Kp = kp
        self.cmd.motorCmd[self.d['FR_0']].Kd = kd
        self.cmd.motorCmd[self.d['FR_0']].tau = 0.0

        self.cmd.motorCmd[self.d['FR_1']].q = angles[4]
        self.cmd.motorCmd[self.d['FR_1']].dq = 0
        self.cmd.motorCmd[self.d['FR_1']].Kp = kp
        self.cmd.motorCmd[self.d['FR_1']].Kd = kd
        self.cmd.motorCmd[self.d['FR_1']].tau = 0.0

        self.cmd.motorCmd[self.d['FR_2']].q = angles[5]
        self.cmd.motorCmd[self.d['FR_2']].dq = 0
        self.cmd.motorCmd[self.d['FR_2']].Kp = kp
        self.cmd.motorCmd[self.d['FR_2']].Kd = kd
        self.cmd.motorCmd[self.d['FR_2']].tau = 0.0

        self.cmd.motorCmd[self.d['FL_0']].q = angles[0]
        self.cmd.motorCmd[self.d['FL_0']].dq = 0
        self.cmd.motorCmd[self.d['FL_0']].Kp = kp
        self.cmd.motorCmd[self.d['FL_0']].Kd = kd
        self.cmd.motorCmd[self.d['FL_0']].tau = 0.0

        self.cmd.motorCmd[self.d['FL_1']].q = angles[1]
        self.cmd.motorCmd[self.d['FL_1']].dq = 0
        self.cmd.motorCmd[self.d['FL_1']].Kp = kp
        self.cmd.motorCmd[self.d['FL_1']].Kd = kd
        self.cmd.motorCmd[self.d['FL_1']].tau = 0.0

        self.cmd.motorCmd[self.d['FL_2']].q = angles[2]
        self.cmd.motorCmd[self.d['FL_2']].dq = 0
        self.cmd.motorCmd[self.d['FL_2']].Kp = kp
        self.cmd.motorCmd[self.d['FL_2']].Kd = kd
        self.cmd.motorCmd[self.d['FL_2']].tau = 0.0

        self.cmd.motorCmd[self.d['RR_0']].q = angles[9]
        self.cmd.motorCmd[self.d['RR_0']].dq = 0
        self.cmd.motorCmd[self.d['RR_0']].Kp = kp
        self.cmd.motorCmd[self.d['RR_0']].Kd = kd
        self.cmd.motorCmd[self.d['RR_0']].tau = 0.0

        self.cmd.motorCmd[self.d['RR_1']].q = angles[10]
        self.cmd.motorCmd[self.d['RR_1']].dq = 0
        self.cmd.motorCmd[self.d['RR_1']].Kp = kp
        self.cmd.motorCmd[self.d['RR_1']].Kd = kd
        self.cmd.motorCmd[self.d['RR_1']].tau = 0.0

        self.cmd.motorCmd[self.d['RR_2']].q = angles[11]
        self.cmd.motorCmd[self.d['RR_2']].dq = 0
        self.cmd.motorCmd[self.d['RR_2']].Kp = kp
        self.cmd.motorCmd[self.d['RR_2']].Kd = kd
        self.cmd.motorCmd[self.d['RR_2']].tau = 0.0

        self.cmd.motorCmd[self.d['RL_0']].q = angles[6]
        self.cmd.motorCmd[self.d['RL_0']].dq = 0
        self.cmd.motorCmd[self.d['RL_0']].Kp = kp
        self.cmd.motorCmd[self.d['RL_0']].Kd = kd
        self.cmd.motorCmd[self.d['RL_0']].tau = 0.0

        self.cmd.motorCmd[self.d['RL_1']].q = angles[7]
        self.cmd.motorCmd[self.d['RL_1']].dq = 0
        self.cmd.motorCmd[self.d['RL_1']].Kp = kp
        self.cmd.motorCmd[self.d['RL_1']].Kd = kd
        self.cmd.motorCmd[self.d['RL_1']].tau = 0.0

        self.cmd.motorCmd[self.d['RL_2']].q = angles[8]
        self.cmd.motorCmd[self.d['RL_2']].dq = 0
        self.cmd.motorCmd[self.d['RL_2']].Kp = kp
        self.cmd.motorCmd[self.d['RL_2']].Kd = kd
        self.cmd.motorCmd[self.d['RL_2']].tau = 0.0

class ROSAgent2(Agent2):
    def __init__(self, path):
        super().__init__(path)

        rospy.init_node("robot_data_publisher", anonymous=True)

        # ROS publishers
        self.base_ang_vel_pub = rospy.Publisher('/base_ang_vel', Float32MultiArray, queue_size=10)
        self.dof_vel_pub = rospy.Publisher('/dof_vel', Float32MultiArray, queue_size=10)
        self.dof_pos_pub = rospy.Publisher('/dof_pos', Float32MultiArray, queue_size=10)
        self.dof_tau_pub = rospy.Publisher('/dof_tau', Float32MultiArray, queue_size=10)
        self.lin_acc_pub = rospy.Publisher('/lin_acc', Float32MultiArray, queue_size=10)

        # 데이터 퍼블리시 스레드 종료 플래그
        self.stop_publish = threading.Event()

        # 데이터 잠금용 Lock
        self.lock = threading.Lock()

    def publish_ros_data(self):
        """
        ROS 데이터 퍼블리시를 500Hz로 수행
        """
        rate = rospy.Rate(500)  # 500 Hz
        while not self.stop_publish.is_set():
            with self.lock:  # 데이터를 안전하게 읽기 위해 Lock 사용
                # base_ang_vel 퍼블리시
                base_ang_vel_msg = Float32MultiArray()
                base_ang_vel_msg.data = self.state.imu.gyroscope
                self.base_ang_vel_pub.publish(base_ang_vel_msg)

                # dof_vel 퍼블리시
                dof_vel_msg = Float32MultiArray()
                dof_vel_msg.data = self.getJointVelocity()
                self.dof_vel_pub.publish(dof_vel_msg)

                # dof_pos 퍼블리시
                dof_pos_msg = Float32MultiArray()
                dof_pos_msg.data = self.getJointPos()
                self.dof_pos_pub.publish(dof_pos_msg)

                # dof_tau 퍼블리시
                dof_tau_msg = Float32MultiArray()
                dof_tau_msg.data = self.getJointTorque()
                self.dof_tau_pub.publish(dof_tau_msg)

                # lin_acc 퍼블리시
                lin_acc_msg = Float32MultiArray()
                lin_acc_msg.data = self.state.imu.accelerometer
                self.lin_acc_pub.publish(lin_acc_msg)

            rate.sleep()

    def start_publishing_thread(self):
        """
        데이터 퍼블리시 스레드를 시작
        """
        self.publish_thread = threading.Thread(target=self.publish_ros_data)
        self.publish_thread.start()

    def stop_publishing_thread(self):
        """
        데이터 퍼블리시 스레드를 종료
        """
        self.stop_publish.set()
        self.publish_thread.join()

    def init_pose(self):
        """
        기존 제어 루프: 50Hz로 실행
        """
        # 퍼블리시 스레드 시작
        self.start_publishing_thread()

        while self.init:
            with self.lock:  # 데이터를 안전하게 처리하기 위해 Lock 사용
                self.pre_step()
                self.get_observations()
            self.motiontime += 1
            if self.motiontime < 100:
                self.setJointValues(self.default_angles, kp=5, kd=1)
            else:
                self.setJointValues(self.default_angles, kp=50, kd=5)
            if self.motiontime > 1100:
                self.init = False
            self.post_step()

        print("Starting")
        while not rospy.is_shutdown():  # ROS 종료 신호가 들어오면 종료
            with self.lock:
                self.step()

        # 퍼블리시 스레드 종료
        self.stop_publishing_thread()