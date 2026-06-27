bl_info = {
    "name": "Phone Camera Control v3",
    "author": "Custom",
    "version": (3, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Phone Cam",
    "description": "Phone gyro camera control - rotation + movement + record + zoom",
    "category": "Camera",
}

import bpy
import threading
import json
import math
import socket
from mathutils import Quaternion, Vector

# ── WebSocket ──────────────────────────────────

def _ws_handshake(conn):
    import hashlib, base64
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk: return False
        data += chunk
    key = None
    for line in data.decode("utf-8", errors="replace").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip(); break
    if not key: return False
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = __import__("base64").b64encode(__import__("hashlib").sha1((key+magic).encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "+accept+"\r\n\r\n").encode())
    return True

def _ws_recv_frame(conn):
    try:
        header = b""
        while len(header) < 2:
            b = conn.recv(2-len(header))
            if not b: return None
            header += b
        b1,b2 = header[0],header[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if opcode == 8: return None
        if opcode not in (1,2): return None
        if length == 126:
            raw = b""
            while len(raw)<2: raw += conn.recv(2-len(raw))
            length = int.from_bytes(raw,"big")
        elif length == 127:
            raw = b""
            while len(raw)<8: raw += conn.recv(8-len(raw))
            length = int.from_bytes(raw,"big")
        mask_key = b""
        if masked:
            while len(mask_key)<4: mask_key += conn.recv(4-len(mask_key))
        payload = b""
        while len(payload)<length:
            chunk = conn.recv(length-len(payload))
            if not chunk: return None
            payload += chunk
        if masked:
            payload = bytes(payload[i]^mask_key[i%4] for i in range(len(payload)))
        return payload.decode("utf-8", errors="replace")
    except: return None

# ── Global state ───────────────────────────────

_server_thread = None
_server_socket = None
_running = False
_latest_data = {}
_data_lock = threading.Lock()
_client_count = 0

# Record state
_is_recording = False
_record_frame = 0
_record_start_frame = 0
_record_fps = 24

# BEST AXIS FIX: portrait phone → Blender camera
# 90° around X axis = Quaternion(cos45, sin45, 0, 0)
AXIS_FIX = Quaternion((0.7071068, 0.7071068, 0.0, 0.0))

def _client_handler(conn, addr):
    global _client_count
    if not _ws_handshake(conn):
        conn.close(); return
    _client_count += 1
    try:
        while _running:
            msg = _ws_recv_frame(conn)
            if msg is None: break
            try:
                with _data_lock:
                    _latest_data.update(json.loads(msg))
            except: pass
    except: pass
    finally:
        _client_count -= 1
        conn.close()

def _server_loop(port):
    global _server_socket, _running
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _server_socket.bind(("0.0.0.0", port))
        _server_socket.listen(5)
        _server_socket.settimeout(1.0)
        while _running:
            try:
                conn, addr = _server_socket.accept()
                threading.Thread(target=_client_handler, args=(conn,addr), daemon=True).start()
            except socket.timeout: continue
    except Exception as e:
        print(f"[PhoneCam] {e}")
    finally:
        _server_socket.close()

def start_server(port=8765):
    global _server_thread, _running
    if _running: return
    _running = True
    _server_thread = threading.Thread(target=_server_loop, args=(port,), daemon=True)
    _server_thread.start()

def stop_server():
    global _running
    _running = False

# ── Timer: apply data every frame ──────────────

def _apply_to_camera():
    global _is_recording, _record_frame, _record_start_frame, _record_fps

    try:
        props = bpy.context.scene.phone_cam_props
    except: return 0.016

    with _data_lock:
        data = dict(_latest_data)
        # Clear one-shot signals
        for key in ("record_start","record_stop","reset"):
            _latest_data.pop(key, None)

    if not data:
        return props.update_interval

    # Get camera object
    obj = bpy.data.objects.get(props.camera_name) if props.camera_name else None
    if obj is None:
        obj = bpy.context.scene.camera
    if obj is None:
        return props.update_interval

    # ── RESET ──
    if data.get("reset"):
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = Quaternion((1,0,0,0))
        obj.location = Vector((0,-10,2))
        cam = obj.data if obj.type == 'CAMERA' else None
        if cam:
            cam.lens = 50.0
        return props.update_interval

    # ── ROTATION ──
    if "qw" in data:
        qw = data["qw"]; qx = data["qx"]; qy = data["qy"]; qz = data["qz"]

        # Phone quaternion (already smoothed on phone side with 0.18 factor)
        phone_q = Quaternion((qw, qx, qy, qz))

        # BEST AXIS FIX: convert portrait phone space → Blender camera space
        target_q = AXIS_FIX @ phone_q

        obj.rotation_mode = "QUATERNION"

        # Blender-side stabilization (light, phone already smoothed)
        if props.use_smoothing:
            cur = obj.rotation_quaternion.copy()
            obj.rotation_quaternion = cur.slerp(target_q, props.smoothing)
        else:
            obj.rotation_quaternion = target_q

    # ── MOVEMENT (joystick) ──
    move_x = float(data.get("move_x", 0.0))
    move_y = float(data.get("move_y", 0.0))
    move_z = float(data.get("move_z", 0.0))
    speed  = props.move_speed

    if abs(move_x) > 0.01 or abs(move_y) > 0.01 or abs(move_z) > 0.01:
        rot = obj.rotation_quaternion.to_matrix() if obj.rotation_mode == "QUATERNION" else obj.rotation_euler.to_matrix()
        # Camera local: right=X, up=Y(world Z), forward=-Z
        local = Vector((move_x * speed, move_z * speed, -move_y * speed))
        obj.location = obj.location + rot @ local

    # ── ZOOM / FOCAL LENGTH ──
    if obj.type == 'CAMERA':
        cam = obj.data
        if "zoom_preset" in data:
            preset = data["zoom_preset"]
            focal_map = {1: 24.0, 3: 70.0, 5: 135.0}
            cam.lens = focal_map.get(preset, 50.0)
        if "focal_delta" in data:
            cam.lens = max(1.0, min(800.0, cam.lens + float(data["focal_delta"])))
        if "focal_set" in data:
            cam.lens = max(1.0, min(800.0, float(data["focal_set"])))
    elif "zoom_preset" in data or "focal_delta" in data:
        # Try to find camera data from scene camera
        scene_cam = bpy.context.scene.camera
        if scene_cam and scene_cam.type == 'CAMERA':
            cam = scene_cam.data
            if "zoom_preset" in data:
                focal_map = {1: 24.0, 3: 70.0, 5: 135.0}
                cam.lens = focal_map.get(data["zoom_preset"], 50.0)
            if "focal_delta" in data:
                cam.lens = max(1.0, min(800.0, cam.lens + float(data["focal_delta"])))

    # ── RECORD ──
    if data.get("record_start") and not _is_recording:
        _is_recording = True
        _record_frame = 0
        _record_fps = int(data.get("fps", 24))
        _record_start_frame = bpy.context.scene.frame_current
        bpy.context.scene.render.fps = _record_fps
        props.is_recording = True
        props.rec_frame_count = 0
        print(f"[PhoneCam] ⏺ Recording at {_record_fps} fps from frame {_record_start_frame}")

    if data.get("record_stop") and _is_recording:
        _is_recording = False
        props.is_recording = False
        print(f"[PhoneCam] ⏹ Stopped — {_record_frame} frames")

    # Insert keyframes EVERY tick while recording
    # CRITICAL FIX: insert BEFORE moving to next frame, and force update
    if _is_recording:
        scene = bpy.context.scene
        frame = _record_start_frame + _record_frame

        # Set frame first
        scene.frame_set(frame)

        # Force object update
        obj.update_tag()
        bpy.context.view_layer.update()

        # Insert keyframes for current position/rotation
        obj.keyframe_insert(data_path="location", frame=frame)
        if obj.rotation_mode == "QUATERNION":
            obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        else:
            obj.keyframe_insert(data_path="rotation_euler", frame=frame)

        _record_frame += 1
        props.rec_frame_count = _record_frame

    # Redraw viewport
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()

    return props.update_interval


_timer_registered = False

def register_timer():
    global _timer_registered
    if not _timer_registered:
        bpy.app.timers.register(_apply_to_camera, persistent=True)
        _timer_registered = True

def unregister_timer():
    global _timer_registered
    if _timer_registered and bpy.app.timers.is_registered(_apply_to_camera):
        bpy.app.timers.unregister(_apply_to_camera)
    _timer_registered = False

# ── Properties ─────────────────────────────────

class PhoneCamProps(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="Port", default=8765, min=1024, max=65535)
    camera_name: bpy.props.StringProperty(name="Camera", default="")
    update_interval: bpy.props.FloatProperty(
        name="Update Interval (s)", default=0.016, min=0.008, max=0.1,
        description="0.016 = 60fps update rate")
    use_smoothing: bpy.props.BoolProperty(name="Blender Smoothing", default=True)
    smoothing: bpy.props.FloatProperty(
        name="Slerp Factor", default=0.18, min=0.01, max=1.0,
        description="0.18 = best match with phone filter. Higher = more responsive")
    move_speed: bpy.props.FloatProperty(
        name="Move Speed", default=0.05, min=0.001, max=2.0)
    is_running: bpy.props.BoolProperty(default=False)
    is_recording: bpy.props.BoolProperty(default=False)
    rec_frame_count: bpy.props.IntProperty(default=0)
    record_fps: bpy.props.EnumProperty(
        name="FPS",
        items=[("24","24",""),("30","30",""),("60","60","")],
        default="24")

# ── Operators ──────────────────────────────────

class PHONECAM_OT_Start(bpy.types.Operator):
    bl_idname = "phonecam.start"; bl_label = "Start Server"
    def execute(self, context):
        props = context.scene.phone_cam_props
        start_server(props.port); register_timer()
        props.is_running = True
        self.report({"INFO"}, f"Server on port {props.port}")
        return {"FINISHED"}

class PHONECAM_OT_Stop(bpy.types.Operator):
    bl_idname = "phonecam.stop"; bl_label = "Stop Server"
    def execute(self, context):
        global _is_recording
        stop_server(); unregister_timer()
        _is_recording = False
        props = context.scene.phone_cam_props
        props.is_running = False; props.is_recording = False
        return {"FINISHED"}

class PHONECAM_OT_Reset(bpy.types.Operator):
    bl_idname = "phonecam.reset"; bl_label = "Reset Camera"
    def execute(self, context):
        props = context.scene.phone_cam_props
        obj = bpy.data.objects.get(props.camera_name) if props.camera_name else context.scene.camera
        if obj:
            obj.rotation_mode = "QUATERNION"
            obj.rotation_quaternion = Quaternion((1,0,0,0))
            obj.location = Vector((0,-10,2))
            if obj.type == 'CAMERA':
                obj.data.lens = 50.0
        with _data_lock: _latest_data.clear()
        return {"FINISHED"}

class PHONECAM_OT_StartRec(bpy.types.Operator):
    bl_idname = "phonecam.start_rec"; bl_label = "Start Record"
    def execute(self, context):
        global _is_recording, _record_frame, _record_start_frame, _record_fps
        props = context.scene.phone_cam_props
        _record_fps = int(props.record_fps)
        _record_frame = 0
        _record_start_frame = context.scene.frame_current
        context.scene.render.fps = _record_fps
        _is_recording = True
        props.is_recording = True
        props.rec_frame_count = 0
        self.report({"INFO"}, f"Recording {_record_fps}fps from frame {_record_start_frame}")
        return {"FINISHED"}

class PHONECAM_OT_StopRec(bpy.types.Operator):
    bl_idname = "phonecam.stop_rec"; bl_label = "Stop Record"
    def execute(self, context):
        global _is_recording
        _is_recording = False
        props = context.scene.phone_cam_props
        props.is_recording = False
        self.report({"INFO"}, f"Recorded {props.rec_frame_count} frames")
        return {"FINISHED"}

# ── Panel ──────────────────────────────────────

class PHONECAM_PT_Panel(bpy.types.Panel):
    bl_label = "📱 Phone Camera v3"
    bl_idname = "PHONECAM_PT_Panel"
    bl_space_type = "VIEW_3D"; bl_region_type = "UI"; bl_category = "Phone Cam"

    def draw(self, context):
        layout = self.layout
        props = context.scene.phone_cam_props

        # Status
        box = layout.box(); row = box.row()
        if props.is_running:
            row.label(text="● LIVE", icon="RADIOBUT_ON")
            row.label(text=f"Port:{props.port}  C:{_client_count}")
        else:
            row.label(text="○ Offline", icon="RADIOBUT_OFF")

        # Settings
        col = layout.column(align=True)
        col.prop(props, "port")
        col.prop_search(props, "camera_name", bpy.data, "objects", text="Camera", icon="CAMERA_DATA")
        col.prop(props, "update_interval")

        # Start/Stop
        row = layout.row()
        if not props.is_running:
            row.operator("phonecam.start", text="▶ Start", icon="PLAY")
        else:
            row.operator("phonecam.stop", text="■ Stop", icon="PAUSE")

        layout.separator()

        # Smoothing
        sb = layout.box()
        sb.label(text="Rotation Smoothing:", icon="MOD_SMOOTH")
        sb.prop(props, "use_smoothing")
        if props.use_smoothing:
            sb.prop(props, "smoothing", slider=True)
            sb.label(text="Best value: 0.18", icon="INFO")

        # Movement
        mb = layout.box()
        mb.label(text="Movement:", icon="ORIENTATION_GLOBAL")
        mb.prop(props, "move_speed", slider=True)

        layout.separator()

        # Focal length display
        obj = bpy.data.objects.get(props.camera_name) if props.camera_name else context.scene.camera
        if obj and obj.type == 'CAMERA':
            fb = layout.box()
            fb.label(text="Focal Length:", icon="CAMERA_DATA")
            fb.prop(obj.data, "lens", text="mm")

        layout.separator()

        # Record
        rb = layout.box()
        rb.label(text="Record Keyframes:", icon="REC")
        rb.prop(props, "record_fps", expand=True)
        if not props.is_recording:
            rb.operator("phonecam.start_rec", text="⏺ Start Record", icon="REC")
        else:
            row2 = rb.row(); row2.alert = True
            row2.operator("phonecam.stop_rec", text=f"⏹ Stop  ({props.rec_frame_count} frames)", icon="SNAP_FACE")

        layout.separator()
        layout.operator("phonecam.reset", text="↺ Reset Camera", icon="LOOP_BACK")

        # IP
        ib = layout.box(); ib.label(text="Connect phone to:", icon="INFO")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8",80)); ip = s.getsockname()[0]; s.close()
        except: ip = "your-PC-IP"
        ib.label(text=f"ws://{ip}:{props.port}")

# ── Register ───────────────────────────────────

classes = [
    PhoneCamProps,
    PHONECAM_OT_Start, PHONECAM_OT_Stop, PHONECAM_OT_Reset,
    PHONECAM_OT_StartRec, PHONECAM_OT_StopRec,
    PHONECAM_PT_Panel,
]

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.phone_cam_props = bpy.props.PointerProperty(type=PhoneCamProps)

def unregister():
    stop_server(); unregister_timer()
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.phone_cam_props

if __name__ == "__main__": register()
