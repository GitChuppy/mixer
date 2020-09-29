# GPLv3 License
#
# Copyright (C) 2020 Ubisoft
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
This module defines Blender handlers for Mixer.

The most important handlers are the depsgraph_update_post and frame_change_post that triggers
the construction of update commands to send to the server.

We also defines undo and redo handlers but these are buggy right now. We'll address their stability in the future.

The load_post handler is not implemented yet and should probably be defined in another module because its behavior is
not related to sending updates to the server. If we do that this module could be renamed "update_handlers.py" to clarify
its purpose.
"""

import logging

import bpy

from mixer.share_data import share_data
from mixer import handlers_generic as generic
from mixer.blender_client import collection as collection_api
from mixer.blender_client import object_ as object_api
from mixer.blender_client import scene as scene_api
import mixer.shot_manager as shot_manager
from mixer.stats import stats_timer
import itertools
from typing import Mapping, Any
from uuid import uuid4

from bpy.app.handlers import persistent

from mixer.share_data import object_visibility
from mixer.stats import StatsTimer
from mixer.draw_handlers import remove_draw_handlers
from mixer.blender_client.client import update_params

logger = logging.getLogger(__name__)


class HandlerManager:
    """Manages Blender handlers activation state

    HandlerManager.set_handlers(wanted_state) will enable or disable the handlers activation and
    should be used for initial or final state handling when enterring or leaving a room.

    Temporary activation or desactivation should be performed using
        with HandlerManager.set_handlers(wanted_state):
            do_something()
    """

    _current_state = False

    def __init__(self, wanted_state: bool):
        self._wanted_state = wanted_state
        self._enter_state = None

    def __enter__(self):
        self._enter_state = HandlerManager._current_state
        if self._wanted_state != self._enter_state:
            self._set_handlers(self._wanted_state)
            HandlerManager._current_state = self._wanted_state
        return self

    def __exit__(self, *exc):
        if self._current_state != self._enter_state:
            self._set_handlers(self._enter_state)
            HandlerManager._current_state = self._enter_state
        return False

    @classmethod
    def _set_handlers(cls, connect: bool):
        try:
            if connect:
                bpy.app.handlers.frame_change_post.append(handler_send_frame_changed)
                bpy.app.handlers.depsgraph_update_post.append(handler_send_scene_data_to_server)
                bpy.app.handlers.undo_pre.append(handler_on_undo_redo_pre)
                bpy.app.handlers.redo_pre.append(handler_on_undo_redo_pre)
                bpy.app.handlers.undo_post.append(handler_on_undo_redo_post)
                bpy.app.handlers.redo_post.append(handler_on_undo_redo_post)
                bpy.app.handlers.load_post.append(handler_on_load)
            else:
                bpy.app.handlers.load_post.remove(handler_on_load)
                bpy.app.handlers.frame_change_post.remove(handler_send_frame_changed)
                bpy.app.handlers.depsgraph_update_post.remove(handler_send_scene_data_to_server)
                bpy.app.handlers.undo_pre.remove(handler_on_undo_redo_pre)
                bpy.app.handlers.redo_pre.remove(handler_on_undo_redo_pre)
                bpy.app.handlers.undo_post.remove(handler_on_undo_redo_post)
                bpy.app.handlers.redo_post.remove(handler_on_undo_redo_post)

                remove_draw_handlers()
        except Exception as e:
            logger.error("Exception during set_handlers(%s) : %s", connect, e)

    @classmethod
    def set_handlers(cls, wanted_state: bool):
        if wanted_state != cls._current_state:
            cls._set_handlers(wanted_state)
            cls._current_state = wanted_state


@persistent
def handler_send_frame_changed(scene):
    logger.debug("handler_send_frame_changed")
    if share_data.client.block_signals:
        logger.debug("handler_send_frame_changed canceled (block_signals = True)")
        return

    share_data.client.synced_time_messages = True
    try:
        send_frame_changed(scene)
    finally:
        share_data.client.send_command_pack()


processing_depsgraph_handler = False


@persistent
def handler_send_scene_data_to_server(scene, dummy):
    global processing_depsgraph_handler
    if processing_depsgraph_handler:
        logger.error("Depsgraph handler recursion attempt")
        return

    processing_depsgraph_handler = True
    try:
        logger.debug("handler_send_scene_data_to_server")

        # Ensure we will rebuild accessors when a depsgraph update happens
        # todo investigate why we need this...
        share_data.set_dirty()

        if share_data.client.block_signals:
            logger.debug("handler_send_scene_data_to_server canceled (block_signals = True)")
            return

        if share_data.use_experimental_sync():
            generic.send_scene_data_to_server(scene, dummy)
        else:
            send_scene_data_to_server(scene, dummy)
    finally:
        processing_depsgraph_handler = False


class TransformStruct:
    def __init__(self, translate, quaternion, scale, visible):
        self.translate = translate
        self.quaternion = quaternion
        self.scale = scale
        self.visible = visible


def update_transform(obj):
    share_data.client.send_transform(obj)


def update_frame_start_end():
    if bpy.context.scene.frame_start != share_data.start_frame or bpy.context.scene.frame_end != share_data.end_frame:
        share_data.client.send_frame_start_end(bpy.context.scene.frame_start, bpy.context.scene.frame_end)
        share_data.start_frame = bpy.context.scene.frame_start
        share_data.end_frame = bpy.context.scene.frame_end


@persistent
def handler_on_load(scene):
    logger.info("handler_on_load")
    pass


def get_scene(scene_name):
    return share_data.blender_scenes.get(scene_name)


def get_collection(collection_name):
    """
    May only return a non master collection
    """
    return share_data.blender_collections.get(collection_name)


def get_parent_collections(collection_name):
    """
    May return a master or non master collection
    """
    parents = []
    for col in share_data.blender_collections.values():
        children_names = {x.name_full for x in col.children}
        if collection_name in children_names:
            parents.append(col)
    return parents


def find_renamed(items_before: Mapping[Any, Any], items_after: Mapping[Any, Any]):
    """
    Split before/after mappings into added/removed/renamed

    Rename detection is based on the mapping keys (e.g. uuids)
    """
    uuids_before = {uuid for uuid in items_before.keys()}
    uuids_after = {uuid for uuid in items_after.keys()}
    renamed_uuids = {uuid for uuid in uuids_after & uuids_before if items_before[uuid] != items_after[uuid]}
    added_items = [items_after[uuid] for uuid in uuids_after - uuids_before - renamed_uuids]
    removed_items = [items_before[uuid] for uuid in uuids_before - uuids_after - renamed_uuids]
    renamed_items = [(items_before[uuid], items_after[uuid]) for uuid in renamed_uuids]
    return added_items, removed_items, renamed_items


def update_scenes_state():
    """
    Must be called before update_collections_state so that non empty collections added to master
    collection are processed
    """

    for scene in share_data.blender_scenes.values():
        if not scene.mixer_uuid:
            scene.mixer_uuid = str(uuid4())

    scenes_after = {
        scene.mixer_uuid: name for name, scene in share_data.blender_scenes.items() if name != "_mixer_to_be_removed_"
    }
    scenes_before = {
        scene.mixer_uuid: name for name, scene in share_data.scenes_info.items() if name != "_mixer_to_be_removed_"
    }
    share_data.scenes_added, share_data.scenes_removed, share_data.scenes_renamed = find_renamed(
        scenes_before, scenes_after
    )

    for old_name, new_name in share_data.scenes_renamed:
        share_data.scenes_info[new_name] = share_data.scenes_info[old_name]
        del share_data.scenes_info[old_name]

    # walk the old scenes
    for scene_name, scene_info in share_data.scenes_info.items():
        scene = get_scene(scene_name)
        if not scene:
            continue
        scene_name = scene.name_full
        old_children = set(scene_info.children)
        new_children = {x.name_full for x in scene.collection.children}

        for x in new_children - old_children:
            share_data.collections_added_to_scene.add((scene_name, x))

        for x in old_children - new_children:
            share_data.collections_removed_from_scene.add((scene_name, x))

        old_objects = {share_data.objects_renamed.get(x, x) for x in scene_info.objects}
        new_objects = {x.name_full for x in scene.collection.objects}

        added_objects = list(new_objects - old_objects)
        if len(added_objects) > 0:
            share_data.objects_added_to_scene[scene_name] = added_objects

        removed_objects = list(old_objects - new_objects)
        if len(removed_objects) > 0:
            share_data.objects_removed_from_scene[scene_name] = removed_objects

    # now the new scenes (in case of rename)
    for scene_name in share_data.scenes_added:
        scene = get_scene(scene_name)
        if not scene:
            continue
        new_children = {x.name_full for x in scene.collection.children}
        for x in new_children:
            share_data.collections_added_to_scene.add((scene_name, x))

        added_objects = {x.name_full for x in scene.collection.objects}
        if len(added_objects) > 0:
            share_data.objects_added_to_scene[scene_name] = added_objects


def update_collections_state():
    """
    Update non master collection state
    """
    new_collections_names = share_data.blender_collections.keys()
    old_collections_names = share_data.collections_info.keys()

    share_data.collections_added |= new_collections_names - old_collections_names
    share_data.collections_removed |= old_collections_names - new_collections_names

    # walk the old collections
    for collection_name, collection_info in share_data.collections_info.items():
        collection = get_collection(collection_name)
        if not collection:
            continue
        old_children = set(collection_info.children)
        new_children = {x.name_full for x in collection.children}

        for x in new_children - old_children:
            share_data.collections_added_to_collection.add((collection.name_full, x))

        for x in old_children - new_children:
            share_data.collections_removed_from_collection.add((collection_name, x))

        new_objects = {x.name_full for x in collection.objects}
        old_objects = {share_data.objects_renamed.get(x, x) for x in collection_info.objects}

        added_objects = [x for x in new_objects - old_objects]
        if len(added_objects) > 0:
            share_data.objects_added_to_collection[collection_name] = added_objects

        removed_objects = [x for x in old_objects - new_objects]
        if len(removed_objects) > 0:
            share_data.objects_removed_from_collection[collection_name] = removed_objects

    # now the new collections (in case of rename)
    for collection_name in share_data.collections_added:
        collection = get_collection(collection_name)
        if not collection:
            continue
        new_children = {x.name_full for x in collection.children}
        for x in new_children:
            share_data.collections_added_to_collection.add((collection.name_full, x))

        added_objects = {x.name_full for x in collection.objects}
        if len(added_objects) > 0:
            share_data.objects_added_to_collection[collection_name] = added_objects


def update_frame_changed_related_objects_state(old_objects: dict, new_objects: dict):
    for obj_name, matrix in share_data.objects_transforms.items():
        new_obj = share_data.old_objects.get(obj_name)
        if not new_obj:
            continue
        if new_obj.matrix_local != matrix:
            share_data.objects_transformed.add(obj_name)


@stats_timer(share_data)
def update_object_state(old_objects: dict, new_objects: dict):
    stats_timer = share_data.current_stats_timer

    with stats_timer.child("checkobjects_addedAndRemoved"):
        objects = set(new_objects.keys())
        share_data.objects_added = objects - old_objects.keys()
        share_data.objects_removed = old_objects.keys() - objects

    share_data.old_objects = new_objects

    if len(share_data.objects_added) == 1 and len(share_data.objects_removed) == 1:
        share_data.objects_renamed[list(share_data.objects_removed)[0]] = list(share_data.objects_added)[0]
        share_data.objects_added.clear()
        share_data.objects_removed.clear()
        return

    if len(share_data.objects_added) > 1 and len(share_data.objects_removed) > 1:
        logger.error(
            f"more than one object renamed: unsupported{share_data.objects_added} {share_data.objects_removed}"
        )

    for obj_name in share_data.objects_removed:
        if obj_name in share_data.old_objects:
            del share_data.old_objects[obj_name]

    with stats_timer.child("updateObjectsParentingChanged"):
        for obj_name, parent in share_data.objects_parents.items():
            if obj_name not in share_data.old_objects:
                continue
            new_obj = share_data.old_objects[obj_name]
            new_obj_parent = "" if new_obj.parent is None else new_obj.parent.name_full
            if new_obj_parent != parent:
                share_data.objects_reparented.add(obj_name)

    with stats_timer.child("update_objects_visibilityChanged"):
        for obj_name, visibility in share_data.objects_visibility.items():
            new_obj = share_data.old_objects.get(obj_name)
            if not new_obj:
                continue
            if visibility != object_visibility(new_obj):
                share_data.objects_visibility_changed.add(obj_name)

    update_frame_changed_related_objects_state(old_objects, new_objects)


def is_in_object_mode():
    return not hasattr(bpy.context, "active_object") or (
        not bpy.context.active_object or bpy.context.active_object.mode == "OBJECT"
    )


def remove_objects_from_scenes():
    changed = False
    for scene_name, object_names in share_data.objects_removed_from_scene.items():
        for object_name in object_names:
            scene_api.send_remove_object_from_scene(share_data.client, scene_name, object_name)
            changed = True
    return changed


def remove_objects_from_collections():
    """
    Non master collections, actually
    """
    changed = False
    for collection_name, object_names in share_data.objects_removed_from_collection.items():
        for object_name in object_names:
            collection_api.send_remove_object_from_collection(share_data.client, collection_name, object_name)
            changed = True
    return changed


def remove_collections_from_scenes():
    changed = False
    for scene_name, collection_name in share_data.collections_removed_from_scene:
        scene_api.send_remove_collection_from_scene(share_data.client, scene_name, collection_name)
        changed = True
    return changed


def remove_collections_from_collections():
    """
    Non master collections, actually
    """
    changed = False
    for parent_name, child_name in share_data.collections_removed_from_collection:
        collection_api.send_remove_collection_from_collection(share_data.client, parent_name, child_name)
        changed = True
    return changed


def add_scenes():
    changed = False
    for scene in share_data.scenes_added:
        scene_api.send_scene(share_data.client, scene)
        changed = True
    for old_name, new_name in share_data.scenes_renamed:
        scene_api.send_scene_renamed(share_data.client, old_name, new_name)
        changed = True
    return changed


def remove_scenes():
    changed = False
    for scene in share_data.scenes_removed:
        scene_api.send_scene_removed(share_data.client, scene)
        changed = True
    return changed


def remove_collections():
    changed = False
    for collection in share_data.collections_removed:
        collection_api.send_collection_removed(share_data.client, collection)
        changed = True
    return changed


def add_objects():
    changed = False
    for obj_name in share_data.objects_added:
        obj = share_data.blender_objects.get(obj_name)
        if obj:
            update_params(obj)
            changed = True
    return changed


def update_transforms():
    changed = False
    for obj_name in share_data.objects_added:
        obj = share_data.blender_objects.get(obj_name)
        if obj:
            update_transform(obj)
            changed = True
    return changed


def add_collections():
    changed = False
    for item in share_data.collections_added:
        collection_api.send_collection(share_data.client, get_collection(item))
        changed = True
    return changed


def add_collections_to_collections():
    changed = False
    for parent_name, child_name in share_data.collections_added_to_collection:
        collection_api.send_add_collection_to_collection(share_data.client, parent_name, child_name)
        changed = True
    return changed


def add_collections_to_scenes():
    changed = False
    for scene_name, collection_name in share_data.collections_added_to_scene:
        scene_api.send_add_collection_to_scene(share_data.client, scene_name, collection_name)
        changed = True
    return changed


def add_objects_to_collections():
    changed = False
    for collection_name, object_names in share_data.objects_added_to_collection.items():
        for object_name in object_names:
            collection_api.send_add_object_to_collection(share_data.client, collection_name, object_name)
            changed = True
    return changed


def add_objects_to_scenes():
    changed = False
    for scene_name, object_names in share_data.objects_added_to_scene.items():
        for object_name in object_names:
            scene_api.send_add_object_to_scene(share_data.client, scene_name, object_name)
            changed = True
    return changed


def update_collections_parameters():
    changed = False
    for collection in share_data.blender_collections.values():
        info = share_data.collections_info.get(collection.name_full)
        if info:
            layer_collection = share_data.blender_layer_collections.get(collection.name_full)
            temporary_hidden = False
            if layer_collection:
                temporary_hidden = layer_collection.hide_viewport
            if (
                info.temporary_hide_viewport != temporary_hidden
                or info.hide_viewport != collection.hide_viewport
                or info.instance_offset != collection.instance_offset
            ):
                collection_api.send_collection(share_data.client, collection)
                changed = True
    return changed


def delete_scene_objects():
    changed = False
    for obj_name in share_data.objects_removed:
        share_data.client.send_deleted_object(obj_name)
        changed = True
    return changed


def rename_objects():
    changed = False
    for old_name, new_name in share_data.objects_renamed.items():
        share_data.client.send_renamed_objects(old_name, new_name)
        changed = True
    return changed


def update_objects_visibility():
    changed = False
    objects = itertools.chain(share_data.objects_added, share_data.objects_visibility_changed)
    for obj_name in objects:
        if obj_name in share_data.blender_objects:
            obj = share_data.blender_objects[obj_name]
            update_transform(obj)
            object_api.send_object_visibility(share_data.client, obj)
            changed = True
    return changed


def update_objects_transforms():
    # changed = False
    for obj_name in share_data.objects_transformed:
        if obj_name in share_data.blender_objects:
            update_transform(share_data.blender_objects[obj_name])
            # changed = True
    return False  # To allow mesh sending after "apply transform"


def reparent_objects():
    changed = False
    for obj_name in share_data.objects_reparented:
        obj = share_data.blender_objects.get(obj_name)
        if obj:
            update_transform(obj)
            changed = True
    return changed


def create_vrtist_objects():
    """
    VRtist will filter the received messages and handle only the objects that belong to the
    same scene as the one initially synchronized
    """
    scene_objects = {x.name_full: x for x in bpy.context.scene.objects}

    changed = False
    for obj_name in share_data.objects_added:
        obj = scene_objects.get(obj_name)
        if obj:
            scene_api.send_add_object_to_vrtist(share_data.client, bpy.context.scene.name_full, obj.name_full)
            changed = True
    return changed


def update_objects_data():
    depsgraph = bpy.context.evaluated_depsgraph_get()

    if len(depsgraph.updates) == 0:
        return  # Exit here to avoid noise if you want to put breakpoints in this function

    data_container = {}
    data = set()
    transforms = set()

    for update in depsgraph.updates:
        obj = update.id.original
        typename = obj.bl_rna.name

        if typename == "Object":
            if hasattr(obj, "data"):
                if obj.data in data_container:
                    data_container[obj.data].append(obj)
                else:
                    data_container[obj.data] = [obj]
            if obj.name_full not in share_data.objects_transformed:
                transforms.add(obj)

        if (
            typename == "Camera"
            or typename == "Mesh"
            or typename == "Curve"
            or typename == "Text Curve"
            or typename == "Sun Light"
            or typename == "Point Light"
            or typename == "Spot Light"
            or typename == "Grease Pencil"
        ):
            data.add(obj)

        if typename == "Material":
            share_data.client.send_material(obj)

        if typename == "Scene":
            update_frame_start_end()
            shot_manager.update_scene()

    # Send transforms
    for obj in transforms:
        update_transform(obj)

    # Send data (mesh) of objects
    for d in data:
        container = data_container.get(d)
        if not container:
            continue
        for c in container:
            update_params(c)


def send_animated_data():
    animated_camera_set = set()
    animated_light_set = set()
    camera_dict = {}
    light_dict = {}
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for update in depsgraph.updates:
        obj = update.id.original
        typename = obj.bl_rna.name
        if typename == "Object":
            if obj.data and obj.data.animation_data is not None:
                if obj.data.bl_rna.name == "Camera":
                    camera_dict[obj.data.animation_data.action.name_full] = obj
                if (
                    obj.data.bl_rna.name == "Point Light"
                    or obj.data.bl_rna.name == "Sun Light"
                    or obj.data.bl_rna.name == "Spot Light"
                ):
                    light_dict[obj.data.animation_data.action.name_full] = obj
        if typename == "Action":
            if camera_dict.get(obj.name_full):
                animated_camera_set.add(camera_dict[obj.name_full])
            if light_dict.get(obj.name_full):
                animated_light_set.add(light_dict[obj.name_full])

    for camera in animated_camera_set:
        share_data.client.send_camera_attributes(camera)
    for light in animated_light_set:
        share_data.client.send_light_attributes(light)


def send_frame_changed(scene):
    logger.debug("send_frame_changed")

    if not share_data.client:
        logger.debug("send_frame_changed cancelled (no client instance)")
        return

    # We can arrive here because of scene deletion (bpy.ops.scene.delete({'scene': to_remove}) that happens during build_scene)
    # so we need to prevent processing self events
    if share_data.client.skip_next_depsgraph_update:
        share_data.client.skip_next_depsgraph_update = False
        logger.debug("send_frame_changed canceled (skip_next_depsgraph_update = True)")
        return

    if not is_in_object_mode():
        logger.debug("send_frame_changed canceled (not is_in_object_mode)")
        return

    with StatsTimer(share_data, "send_frame_changed") as timer:
        with timer.child("setFrame"):
            if not share_data.client.block_signals:
                share_data.client.send_frame(scene.frame_current)

        with timer.child("clear_lists"):
            share_data.clear_changed_frame_related_lists()

        with timer.child("update_frame_changed_related_objects_state"):
            update_frame_changed_related_objects_state(share_data.old_objects, share_data.blender_objects)

        with timer.child("checkForChangeAndSendUpdates"):
            update_objects_transforms()

        # update for next change
        with timer.child("update_objects_info"):
            share_data.update_objects_info()

        # temporary code :
        # animated parameters are not sent, we need camera & light animated parameters for VRtist
        # (focal lens etc.)
        # please remove this when animation is managed
        with timer.child("send_animated_data"):
            send_animated_data()

        with timer.child("send_current_camera"):
            scene_camera_name = ""
            if bpy.context.scene.camera is not None:
                scene_camera_name = bpy.context.scene.camera.name_full

            if share_data.current_camera != scene_camera_name:
                share_data.current_camera = scene_camera_name
                share_data.client.send_current_camera(share_data.current_camera)

        with timer.child("send_shot_manager_current_shot"):
            shot_manager.send_frame()


@stats_timer(share_data)
def send_scene_data_to_server(scene, dummy):
    logger.debug(
        "send_scene_data_to_server(): skip_next_depsgraph_update %s, pending_test_update %s",
        share_data.client.skip_next_depsgraph_update,
        share_data.pending_test_update,
    )

    timer = share_data.current_stats_timer

    if not share_data.client:
        logger.info("send_scene_data_to_server canceled (no client instance)")
        return

    share_data.set_dirty()
    with timer.child("clear_lists"):
        share_data.clear_lists()

    depsgraph = bpy.context.evaluated_depsgraph_get()
    if depsgraph.updates:
        logger.debug("Current dg updates ...")
        for update in depsgraph.updates:
            logger.debug(" ......%s", update.id.original)

    # prevent processing self events, but always process test updates
    if not share_data.pending_test_update and share_data.client.skip_next_depsgraph_update:
        share_data.client.skip_next_depsgraph_update = False
        logger.debug("send_scene_data_to_server canceled (skip_next_depsgraph_update = True) ...")
        return

    share_data.pending_test_update = False

    if not is_in_object_mode():
        if depsgraph.updates:
            logger.info("send_scene_data_to_server canceled (not is_in_object_mode). Skipping updates")
            for update in depsgraph.updates:
                logger.info(" ......%s", update.id.original)
        return

    update_object_state(share_data.old_objects, share_data.blender_objects)

    with timer.child("update_scenes_state"):
        update_scenes_state()

    with timer.child("update_collections_state"):
        update_collections_state()

    changed = False
    with timer.child("checkForChangeAndSendUpdates"):
        changed |= remove_objects_from_collections()
        changed |= remove_objects_from_scenes()
        changed |= remove_collections_from_collections()
        changed |= remove_collections_from_scenes()
        changed |= remove_collections()
        changed |= remove_scenes()
        changed |= add_scenes()
        changed |= add_collections()
        changed |= add_objects()
        changed |= update_transforms()
        changed |= add_collections_to_scenes()
        changed |= add_collections_to_collections()
        changed |= add_objects_to_collections()
        changed |= add_objects_to_scenes()
        changed |= update_collections_parameters()
        changed |= create_vrtist_objects()
        changed |= delete_scene_objects()
        changed |= rename_objects()
        changed |= update_objects_visibility()
        changed |= update_objects_transforms()
        changed |= reparent_objects()
        changed |= shot_manager.check_montage_mode()

    if not changed:
        with timer.child("update_objects_data"):
            update_objects_data()

    # update for next change
    with timer.child("update_current_data"):
        share_data.update_current_data()

    logger.debug("send_scene_data_to_server: end")


@persistent
def handler_on_undo_redo_pre(scene):
    logger.info("on_undo_redo_pre")
    send_scene_data_to_server(scene, None)


def remap_objects_info():
    # update objects references
    added_objects = set(share_data.blender_objects.keys()) - set(share_data.old_objects.keys())
    removed_objects = set(share_data.old_objects.keys()) - set(share_data.blender_objects.keys())
    # we are only able to manage one object rename
    if len(added_objects) == 1 and len(removed_objects) == 1:
        old_name = list(removed_objects)[0]
        new_name = list(added_objects)[0]

        visible = share_data.objects_visibility[old_name]
        del share_data.objects_visibility[old_name]
        share_data.objects_visibility[new_name] = visible

        parent = share_data.objects_parents[old_name]
        del share_data.objects_parents[old_name]
        share_data.objects_parents[new_name] = parent
        for name, parent in share_data.objects_parents.items():
            if parent == old_name:
                share_data.objects_parents[name] = new_name

        matrix = share_data.objects_transforms[old_name]
        del share_data.objects_transforms[old_name]
        share_data.objects_transforms[new_name] = matrix

    share_data.old_objects = share_data.blender_objects


@stats_timer(share_data)
@persistent
def handler_on_undo_redo_post(scene, dummy):
    logger.info("on_undo_redo_post")

    share_data.set_dirty()
    share_data.clear_lists()
    # apply only in object mode
    if not is_in_object_mode():
        return

    old_objects_name = dict([(k, None) for k in share_data.old_objects.keys()])  # value not needed
    remap_objects_info()
    for k, v in share_data.old_objects.items():
        if k in old_objects_name:
            old_objects_name[k] = v

    update_object_state(old_objects_name, share_data.old_objects)

    update_collections_state()
    update_scenes_state()

    remove_objects_from_scenes()
    remove_objects_from_collections()
    remove_collections_from_scenes()
    remove_collections_from_collections()

    remove_collections()
    remove_scenes()
    add_scenes()
    add_objects()
    add_collections()

    add_collections_to_scenes()
    add_collections_to_collections()

    add_objects_to_collections()
    add_objects_to_scenes()

    update_collections_parameters()
    create_vrtist_objects()
    delete_scene_objects()
    rename_objects()
    update_objects_visibility()
    update_objects_transforms()
    reparent_objects()

    # send selection content (including data)
    materials = set()
    for obj in bpy.context.selected_objects:
        update_transform(obj)
        if hasattr(obj, "data"):
            update_params(obj)
        if hasattr(obj, "material_slots"):
            for slot in obj.material_slots[:]:
                materials.add(slot.material)

    for material in materials:
        share_data.client.send_material(material)

    share_data.update_current_data()
