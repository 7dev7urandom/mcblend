'''
Functions related to exporting animations.
'''
from __future__ import annotations

from typing import NamedTuple, Dict, Optional, List, Tuple, Set
import math
from dataclasses import dataclass, field
from itertools import cycle, tee, islice

import bpy
import bpy_types

import numpy as np
from .json_tools import get_vect_json
from .common import (
    MINECRAFT_SCALE_FACTOR, MCObjType, McblendObjectGroup
)


def _pick_closest_rotation(
        base: np.ndarray, close_to: np.ndarray,
        original_rotation: Optional[np.ndarray] = None
    ) -> np.ndarray:
    '''
    Takes two arrays with euler rotations in degrees. Looks for rotations
    that result in same orientation ad the base rotation. Picks the vector
    which is the closest to the :code:`close_to` using euclidean distance.

    *The :code:`original_rotation` is added specifically to fix some issues with
    bones rotated before the animation. Issue #25 on Github describes the
    problem in detail.

    :base: np.ndarray: the base rotation. Function is looking for different
        representations of this orientation.
    :param close_to: target rotation. Function returns the result as close
        as possible to this vector.
    :param original_rotation: optional - the original rotation of the object
        before the start of the animation.
    :returns: another euler angle that represents the same rotation as the base
        rotation.
    '''
    if original_rotation is None:
        original_rotation = np.array([0.0, 0.0, 0.0])

    def _pick_closet_location(
            base: np.ndarray, close_to: np.ndarray
    ) -> Tuple[float, np.ndarray]:
        choice: np.ndarray = base
        distance = np.linalg.norm(choice - close_to)

        for i in range(3):  # Adds removes 360 to all 3 axis (picks the best)
            arr = np.zeros(3)
            arr[i] = 360
            while choice[i] < close_to[i]:
                new_choice = choice + arr
                new_distance = np.linalg.norm(new_choice - close_to)
                if new_distance > distance:
                    break
                distance, choice = new_distance, new_choice
            while choice[i] > close_to[i]:
                new_choice = choice - arr
                new_distance = np.linalg.norm(new_choice - close_to)
                if new_distance > distance:
                    break
                distance, choice = new_distance, new_choice
        return distance, choice

    distance1, choice1 = _pick_closet_location(base, close_to)
    distance2, choice2 = _pick_closet_location(  # Counterintuitive but works
        (
            base +
            np.array([180, 180 + original_rotation[1] * 2, 180])) *
            np.array([1, -1, 1]
        ),
        close_to
    )
    if distance2 < distance1:
        return choice2
    return choice1

def _get_keyframes(context: bpy_types.Context) -> List[int]:
    '''
    Lists keyframe numbers of the animation from keyframes of NLA tracks and
    actions of selected objects.

    :param context: the context of running the operator.
    :returns: the list of the keyframes for the animation.
    '''
    def get_action_keyframes(action: bpy.types.Action) -> Set[int]:
        '''Gets set of keyframes from an action.'''
        if action.fcurves is None:
            return set()
        result: Set[int] = set()
        for fcurve in action.fcurves:
            if fcurve.keyframe_points is None:
                continue
            for keyframe_point in fcurve.keyframe_points:
                result.add(round(keyframe_point.co[0]))
        return result

    keyframes: Set[int] = set()
    for obj in context.selected_objects:
        if obj.animation_data is None:
            continue
        if obj.animation_data.action is not None:
            keyframes.update(get_action_keyframes(obj.animation_data.action))
        if obj.animation_data.nla_tracks is None:
            continue
        for nla_track in obj.animation_data.nla_tracks:
            if nla_track.mute:
                continue
            for strip in nla_track.strips:
                if strip.type != 'CLIP':
                    continue
                strip_action_keyframes = get_action_keyframes(strip.action)
                # Scale/strip the action data with the strip
                # transformations
                offset =  strip.frame_start
                limit_down =  strip.action_frame_start
                limit_up =  strip.action_frame_end
                scale =  strip.scale
                cycle_length = limit_up - limit_down
                scaled_cycle_length = cycle_length * scale
                repeat =  strip.repeat
                transformed_keyframes: Set[int] = set()
                for keyframe in sorted(strip_action_keyframes):
                    if keyframe < limit_down or keyframe > limit_up:
                        continue
                    transformed_keyframe_base = keyframe * scale
                    for i in range(math.ceil(repeat)):
                        transformed_keyframe = (
                            (i * scaled_cycle_length) +
                            transformed_keyframe_base
                        )
                        if transformed_keyframe/scaled_cycle_length > repeat:
                            # Can happen when we've got for example 4th
                            # repeat but we only need 3.5
                            break
                        transformed_keyframe += offset
                        transformed_keyframes.add(
                            min(round(transformed_keyframe), strip.frame_end))
                keyframes.update(transformed_keyframes)
    return sorted(keyframes)  # Sorted list of ints




class PoseBone(NamedTuple):
    '''Properties of a pose of single bone.'''
    name: str
    location: np.array
    rotation: np.array
    scale: np.array
    parent_name: Optional[str] = None

    def relative(self, original: PoseBone) -> PoseBone:
        '''
        Returns :class:`PoseBone` object with properties of the bone
        relative to the original pose.

        :param original: the original pose.
        '''
        return PoseBone(
            name=self.name, scale=self.scale / original.scale,
            location=self.location - original.location,
            rotation=self.rotation - original.rotation,
            parent_name=original.parent_name
        )

class Pose:
    '''A pose in a frame of animation.'''
    def __init__(self):
        self.pose_bones: Dict[str, PoseBone] = {}
        '''dict of bones in a pose keyed by the name of the bones'''

    def load_poses(
            self, object_properties: McblendObjectGroup
        ):
        '''
        Builds :class:`Pose` object from object properties.

        :param object_properties: group of mcblend objects.
        '''
        for objprop in object_properties.values():
            if objprop.mctype in [MCObjType.BONE, MCObjType.BOTH]:
                # Scale
                local_matrix = objprop.get_local_matrix(
                    objprop.parent, normalize=False)
                scale = np.array(local_matrix.to_scale())[[0, 2, 1]]
                # Location
                location = np.array(local_matrix.to_translation())
                location = location[[0, 2, 1]] * MINECRAFT_SCALE_FACTOR
                # Rotation
                rotation = objprop.get_mcrotation(objprop.parent)
                if objprop.parent is not None:
                    parent_name=objprop.parent.obj_name
                else:
                    parent_name=None
                self.pose_bones[objprop.obj_name] = PoseBone(
                    name=objprop.obj_name, location=location, scale=scale,
                    rotation=rotation, parent_name=parent_name)

@dataclass
class AnimationExport:
    '''
    Object that represents animation during export.

    :param name: Name of the animation.
    :param length: Length of animation in seconds.
    :param loop_animation: Whether the Minecraft animation should be exported
        with loop property set to true.
    :param anim_time_update: Value of anim_time_update property of Minecraft
        animation.
    :param fps: The FPS setting of the scene.
    :param effect_events: The events of the animation from
        OBJECT_NusiqMcblendEventProperties.
    :param original_pose: Optional - the base pose of the animated object.
        The pose is empty by default after object creation until it's loaded.
    :param single_frame: Optional - whether the animation should be exported as
        a single frame pose (True) or as whole animation. False by default.
    :param poses: Optional - poses of the animation (keyframes) keyed by the
        number of the frame. This dictionary is empty by default after the
        creation and it gets populated on loading the poses.
    '''
    name: str
    length: float
    loop_animation: bool
    anim_time_update: str
    fps: float
    effect_events: Dict[str, Tuple[List[Dict], List[Dict]]]
    original_pose: Pose = field(default_factory=Pose)
    single_frame: bool = field(default_factory=bool)  # bool() = False
    poses: Dict[int, Pose] = field(default_factory=dict)
    sound_effects: Dict[int, List[Dict]] = field(default_factory=dict)
    particle_effects: Dict[int, List[Dict]] = field(default_factory=dict)

    def load_poses(
            self, object_properties: McblendObjectGroup,
            context: bpy_types.Context
        ):
        '''
        Populates the poses dictionary of this object.

        :param object_properties: group of mcblend objects.
        :param context: the context of running the operator.
        '''
        original_frame = context.scene.frame_current
        bpy.ops.screen.animation_cancel()
        try:
            context.scene.frame_set(0)
            self.original_pose.load_poses(object_properties)
            if self.single_frame:
                context.scene.frame_set(original_frame)
                pose = Pose()
                pose.load_poses(object_properties)

                # The frame value in the dictionary key doesn't really matter
                self.poses[original_frame] = pose
            else:
                for keyframe in _get_keyframes(context):
                    if (
                        keyframe < context.scene.frame_start or
                        keyframe > context.scene.frame_end
                    ):
                        continue  # skip frames out of range
                    context.scene.frame_set(keyframe)
                    curr_pose = Pose()
                    curr_pose.load_poses(object_properties)
                    self.poses[keyframe] = curr_pose
                # Load sound effects and particle effects
                for timeline_marker in context.scene.timeline_markers:
                    if timeline_marker.name not in self.effect_events:
                        continue
                    sound, particle = self.effect_events[timeline_marker.name]
                    if len(sound) > 0:
                        self.sound_effects[timeline_marker.frame] = sound
                    if len(particle) > 0:
                        self.particle_effects[timeline_marker.frame] = particle
        finally:
            context.scene.frame_set(original_frame)

    def json(
            self, old_json: Optional[Dict]=None,
            skip_rest_poses: bool=True) -> Dict:
        '''
        Returns the JSON dict with Minecraft animation. If JSON dict with
        valid animation file is passed to the function the function
        modifies it's content.

        :param old_json: The original animation file to write into.
        :param skip_rest_poses: If true the exported animation won't contain
            information about bones that remain in the rest pose.
        :returns: JSON dict with Minecraft animation.
        '''
        # Create result dict
        result: Dict = {"format_version": "1.8.0", "animations": {}}
        try:
            if isinstance(old_json['animations'], dict):  # type: ignore
                result: Dict = old_json  # type: ignore
        except (TypeError, LookupError):
            pass

        bones: Dict = {}
        for bone_name in self.original_pose.pose_bones:
            bone = self._json_bone(bone_name, skip_rest_poses)
            if bone != {}:  # Nothing to export
                bones[bone_name] = bone

        if self.single_frame:
            # Other properties don't apply
            result["animations"][f"animation.{self.name}"] = {
                "bones": bones,
                "loop": True
            }
        else:
            result["animations"][f"animation.{self.name}"] = {
                "animation_length": self.length,
                "bones": bones
            }
            if len(self.particle_effects) > 0:
                particle_effects = {}
                for key_frame, value in self.particle_effects.items():
                    timestamp = str(round((key_frame-1) / self.fps, 4))
                    particle_effects[timestamp] = value
                result["animations"][f"animation.{self.name}"][
                    'particle_effects'] = particle_effects

            if len(self.sound_effects) > 0:
                sound_effects = {}
                for key_frame, value in self.sound_effects.items():
                    timestamp = str(round((key_frame-1) / self.fps, 4))
                    sound_effects[timestamp] = value
                result["animations"][f"animation.{self.name}"][
                    'sound_effects'] = sound_effects

            data = result["animations"][f"animation.{self.name}"]
            if self.loop_animation:
                data['loop'] = True
            if self.anim_time_update != "":
                data['anim_time_update'] = self.anim_time_update
        return result

    def _json_bone(self, bone_name: str, skip_rest_pose: bool) -> Dict:
        '''
        Returns optimized JSON dict with an animation of single bone.

        :param bone_name: the name of the bone.
        :param skip_rest_pose: whether the properties of the bone being in
            its rest pose should be skipped.
        :returns: the part of animation with animation of a single bone.
        '''
        # t, rot, loc, scale
        poses: List[Dict] = []
        prev_pose_bone = PoseBone(
            name=bone_name, scale=np.zeros(3), location=np.zeros(3),
            rotation=np.zeros(3),
        )
        for key_frame in self.poses:
            # Get relative PoseBone with minimized rotation
            original_pose_bone = self.original_pose.pose_bones[bone_name]
            parent_name = original_pose_bone.parent_name

            # Get original parent scale. Scaling the location with original
            # parent scale allows to have issue #71 fixed and also being able
            # to use scale in animations (issue #76) which was impossible to do
            # after commit 19ef865943da7fde039bba7b7f50d1fa69a140b6 (the one
            # which closed issue #71).
            if parent_name in self.original_pose.pose_bones:
                original_parent_pose_bone = self.original_pose.pose_bones[
                    parent_name]
                original_parent_scale = original_parent_pose_bone.scale
            else:
                original_parent_scale = np.ones(3)
            pose_bone = self.poses[key_frame].pose_bones[bone_name].relative(
                original_pose_bone)
            pose_bone = PoseBone(
                name=pose_bone.name,
                scale=pose_bone.scale,
                location=pose_bone.location * original_parent_scale,
                rotation=_pick_closest_rotation(
                    pose_bone.rotation, prev_pose_bone.rotation,
                    original_pose_bone.rotation)
            )
            timestamp = str(round((key_frame-1) / self.fps, 4))
            poses.append({
                't': timestamp,
                'loc': get_vect_json(pose_bone.location),
                'scl': get_vect_json(pose_bone.scale),
                'rot': get_vect_json(pose_bone.rotation),
            })
            # Update prev pose
            prev_pose_bone = pose_bone

        # Filter unnecessary frames and add them to bone
        if not poses:  # If empty return empty animation
            return {'position': {}, 'rotation': {}, 'scale': {}}
        if self.single_frame:  # Returning single frame pose is easier
            result = {}
            loc, rot, scl = poses[0]['loc'], poses[0]['rot'], poses[0]['scl']
            # Filter rest pose positions
            if loc != [0, 0, 0] or not skip_rest_pose:
                result['position'] = poses[0]['loc']
            if rot != [0, 0, 0] or not skip_rest_pose:
                result['rotation'] = poses[0]['rot']
            if scl != [1, 1, 1] or not skip_rest_pose:
                result['scale'] = poses[0]['scl']
            return result
        bone: Dict = {  # dictionary populated with 0 timestamp frame
            'position': {poses[0]['t']: poses[0]['loc']},
            'rotation': {poses[0]['t']: poses[0]['rot']},
            'scale': {poses[0]['t']: poses[0]['scl']},
        }
        # iterate in threes (previous, current , next), remove unnecessary
        # items
        prev, curr, next_ = tee(poses, 3)
        for prv, crr, nxt in zip(
                prev, islice(curr, 1, None), islice(next_, 2, None)
        ):
            if prv['scl'] != crr['scl'] or crr['scl'] != nxt['scl']:
                bone['scale'][crr['t']] = crr['scl']

            if prv['loc'] != crr['loc'] or crr['loc'] != nxt['loc']:
                bone['position'][crr['t']] = crr['loc']

            if prv['rot'] != crr['rot'] or crr['rot'] != nxt['rot']:
                bone['rotation'][crr['t']] = crr['rot']
        # Add last element unless there is only one (in which case it's already
        # added)
        if len(poses) > 1:
            bone['rotation'][poses[-1]['t']] = poses[-1]['rot']
            bone['position'][poses[-1]['t']] = poses[-1]['loc']
            bone['scale'][poses[-1]['t']] = poses[-1]['scl']
        # Filter rest pose positions
        if skip_rest_pose:
            for v in bone['position'].values():
                if v != [0, 0, 0]:
                    break  # found non-rest pose item
            else:  # this is rest pose
                del bone['position']

            for v in bone['rotation'].values():
                if v != [0, 0, 0]:
                    break  # found non-rest pose item
            else:  # this is rest pose
                del bone['rotation']

            for v in bone['scale'].values():
                if v != [1, 1, 1]:
                    break  # found non-rest pose item
            else:  # this is rest pose
                del bone['scale']
        return bone
