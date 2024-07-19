# Copyright (c) 2023 Hamburg Bit-Bots
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import socket
from typing import Any
import rclpy

from construct import ConstError
from rclpy.node import Node
from rclpy.time import Time
from rclpy.duration import Duration
from std_msgs.msg import Header
from construct import Container
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from game_controller_hl.gamestate import GameStateStruct, ResponseStruct
from game_controller_hl.utils import get_parameters_from_other_node

from game_controller_hl_interfaces.msg import GameState


class GameStateReceiver(Node):
    """ This class puts up a simple UDP Server which receives the
    *addr* parameter to listen to the packages from the game_controller.

    If it receives a package it will be interpreted with the construct data
    structure and the :func:`on_new_gamestate` will be called with the content.

    After this we send a package back to the GC """

    def __init__(self, *args, **kwargs):
        super().__init__('game_controller', *args, **kwargs, allow_undeclared_parameters=True, automatically_declare_parameters_from_overrides=True)

        # Check if we have the team and bot id parameters or if we should get them from the blackboard
        if self.has_parameter('team_id') and self.has_parameter('bot_id'):
            self.get_logger().info('Found team_id and bot_id parameter, using them')
            # Get the parameters from our node
            self.team_number = self.get_parameter('team_id').value
            self.player_number = self.get_parameter('bot_id').value
        else:
            self.get_logger().info('No team_id and bot_id parameter set in game_controller, getting them from blackboard')
            # Get the parameter names from the parameter server
            param_blackboard_name: str = self.get_parameter('parameter_blackboard_name').value
            team_id_param_name: str = self.get_parameter('team_id_param_name').value
            bot_id_param_name: str = self.get_parameter('bot_id_param_name').value
            # Get the parameters from the blackboard
            params = get_parameters_from_other_node(self, param_blackboard_name, [
                team_id_param_name, 
                bot_id_param_name])
            # Set the parameters
            self.team_number = params[team_id_param_name]
            self.player_number = params[bot_id_param_name]

        self.get_logger().info(f'We are playing as player {self.player_number} in team {self.team_number}')

        # The publisher for the game state
        self.state_publisher = self.create_publisher(GameState, 'gamestate', 1)

        #The publisher for the diagnostics
        self.diagnostic_pub = self.create_publisher(DiagnosticArray, "diagnostics", 1)

        # The time in seconds after which we assume the game controller is lost 
        # and we tell the robot to move
        self.game_controller_lost_time = 5

        # The address listening on and the port for sending back the robots meta data
        self.addr = (
            self.get_parameter('listen_host').value, 
            self.get_parameter('listen_port').value
        )
        self.answer_port = self.get_parameter('answer_port').value

        # The time of the last package
        self.last_package_time: Time = self.get_clock().now()

        # Create the socket we want to use for the communications
        self.socket = self._open_socket()

    def _open_socket(self) -> socket.socket:
        """ Creates the socket """
        new_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        new_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        new_socket.bind(self.addr)
        new_socket.settimeout(2)
        return new_socket

    def receive_forever(self):
        """ Waits in a loop for new packages """
        while rclpy.ok():
            # Try to receive a package
            self.receive_and_answer_once()
            # Check if we didn't receive a package for a long time and if so
            # call the fallback behavior
            if self.get_time_since_last_package() > Duration(seconds=self.game_controller_lost_time):
                self.publish_diagnostics(False)
            else:
                self.publish_diagnostics(True)


    def receive_and_answer_once(self):
        """ Receives a package, interprets it and sends an answer. """
        try:
            # Receive the package 
            data, peer = self.socket.recvfrom(GameStateStruct.sizeof())

            # Parse the package based on the GameStateStruct
            # This throws a ConstError if it doesn't work
            parsed_state = GameStateStruct.parse(data)

            # Assign the new package after it parsed successful to the state
            self.last_package_time = self.get_clock().now()

            # Build the game state message and publish it
            self.state_publisher.publish(self.build_game_state_msg(parsed_state))

            # Answer the GameController
            self.answer_to_gamecontroller(peer)

        except AssertionError as ae:
            self.get_logger().error(str(ae))
        except socket.timeout:
            self.get_logger().info("No GameController message received (socket timeout)", throttle_duration_sec=5)
        except ConstError:
            self.get_logger().warn("Parse Error: Probably using an old protocol!")
        except IOError as e:
                self.get_logger().warn(f"Error while sending keep-alive: {str(e)}")

    def publish_diagnostics(self, received_message_lately: bool):
        """ 
        This publishes a Diagnostics Array.
        """
        self.get_logger().info("No GameController message received", throttle_duration_sec=5)

        #initialize DiagnsticArray message
        diag_array = DiagnosticArray()

        #configure DiagnosticStatus message
        diag = DiagnosticStatus(name = "Game Controller", hardware_id = "Game Controller" )
        if not received_message_lately:
            diag.message = "Lost connection to game controller for " + str(int(self.get_time_since_last_package().nanoseconds/1e9)) + " sec"
            diag.level = DiagnosticStatus.WARN
        else:
            diag.message = "Connected"
            diag.level = DiagnosticStatus.OK 

        self.get_logger().warn(str(diag)) 

        diag_array.status.append(diag)    
    
        #add timestamp to header and publish DiagnosticArray
        diag_array.header.stamp = self.get_clock().now().to_msg()
        self.diagnostic_pub.publish(diag_array)


    def answer_to_gamecontroller(self, peer):
        """ Sends a life sign to the game controller """
        # Build the answer package
        data = ResponseStruct.build(dict(
            team=self.team_number,
            player=self.player_number,
            message=2))
        # Send the package
        self.get_logger().debug(f'Sending answer to {peer[0]}:{self.answer_port}')
        try:
            self.socket.sendto(data, (peer[0], self.answer_port))
        except Exception as e:
            self.get_logger().error(f"Network Error: {str(e)}")

    def build_game_state_msg(self, state) -> GameState:
        """ Builds a GameState message from the game state """
        
        # Get the team objects sorted into own and rival team
        own_team = GameStateReceiver.select_team_by(
            lambda team: team.team_number == self.team_number, 
            state.teams)
        rival_team = GameStateReceiver.select_team_by(
            lambda team: team.team_number != self.team_number, 
            state.teams)

        # Add some assertions to make sure everything is fine
        assert not (own_team is None or rival_team is None), \
            f'Team {self.team_number} not playing, only {state.teams[0].team_number} and {state.teams[1].team_number}'
        
        assert self.player_number <= len(own_team.players), \
            f'Robot {self.player_number} not playing'
        
        this_robot = own_team.players[self.player_number - 1]

        return GameState(
            header = Header(stamp=self.get_clock().now().to_msg()),
            game_state = state.game_state.intvalue,
            secondary_state = state.secondary_state.intvalue,
            secondary_state_mode = state.secondary_state_info[1],
            first_half = state.first_half,
            own_score = own_team.score,
            rival_score = rival_team.score,
            seconds_remaining = state.seconds_remaining,
            secondary_seconds_remaining = state.secondary_seconds_remaining,
            has_kick_off = state.kick_of_team == self.team_number,
            penalized = this_robot.penalty != 0,
            seconds_till_unpenalized = this_robot.secs_till_unpenalized,
            secondary_state_team = state.secondary_state_info[0],
            team_color = own_team.team_color.intvalue,
            drop_in_team = state.drop_in_team,
            drop_in_time = state.drop_in_time,
            penalty_shot = own_team.penalty_shot,
            single_shots = own_team.single_shots,
            coach_message = own_team.coach_message,
            team_mates_with_penalty = [player.penalty != 0 for player in own_team.players],
            team_mates_with_red_card = [player.number_of_red_cards != 0 for player in own_team.players],
        )

    def get_time_since_last_package(self) -> Duration:
        """ Returns the time in seconds since the last package was received """
        return self.get_clock().now() - self.last_package_time

    @staticmethod
    def select_team_by(predicate: callable, teams: list[Container]) -> Container:
        """ Selects the team based on the predicate """
        selected = [team for team in teams if predicate(team)]
        return next(iter(selected), None)


def main(args=None):
    rclpy.init(args=args)
    receiver = GameStateReceiver()

    try:
        receiver.receive_forever()
    except KeyboardInterrupt:
        receiver.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
