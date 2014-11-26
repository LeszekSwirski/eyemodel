#!/usr/bin/python
# coding=utf-8

import sys
import os
import math
import collections
import subprocess
import tempfile
import shutil
import time
import traceback
import threading
import re
try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty  # python 3.x

SCRIPT_PATH = sys.arg[0] if __name__ == "__main__" else __file__
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)

MODEL_PATH = os.path.join(SCRIPT_DIR, "Swirski-EyeModel.blend")
TEXTURE_PATH = os.path.join(SCRIPT_DIR, "textures")
BLENDER_SCRIPT_TEMPLATE = os.path.join(SCRIPT_DIR, "blender_script.py.template")

RENDER_LINE_RE = re.compile(r"""
    Fra: \s* (?P<frame>\d+) \s*                     # Frame number
    Mem: \s* (?P<mem>[\d.]+\S) \s*                  # Memory use
        \(
        [\d.]+\S \s*,\s*
        Peak \s* (?P<mempeak>[\d.]+\S)              # Peak memory
        \)
    \s*\|\s*
    Remaining: \s* (?P<rem>[\d:.]+)                 # Remaining time
    \s*\|\s*
    Mem: \s* (?P<mem2>[\d.]+\S) \s*,\s*             # More memory use?
    Peak: \s* (?P<mempeak2>[\d.]+\S)                # And more peak memory?
    \s*\|\s*
    (?P<rig>[^,|]+) \s*,\s* (?P<layer>[^,|]+)       # Rig and layer name
    \s*\|\s*
    Path\ Tracing\ Tile \s+
        (?P<tile>\d+)/(?P<tiles>\d+)                # Tile num
    \s*,\s*
    Sample \s+
        (?P<sample>\d+)/(?P<samples>\d+)            # Sample num
    \s*
""", flags=re.VERBOSE)


def get_blender_path():
    def isexecutable(path):
        return os.path.isfile(path) and os.access(path, os.X_OK)

    # Get blender from environment if it's set
    if "BLENDER_PATH" in os.environ:
        path = os.environ["BLENDER_PATH"]
        if isexecutable(path):
            return path

    # If blender is on the path, just rely on the OS's path search
    if isexecutable("blender"):
        return "blender"

    # Try some default values
    if sys.platform == "win32":
        paths = ["C:/Program Files/Blender Foundation/Blender/blender.exe",
                 "C:/Program Files (x86)/Blender Foundation/Blender/blender.exe"]
    else:
        paths = ["/usr/local/bin/blender", "/usr/bin/blender", "/bin/blender",
                 "/Applications/Blender/blender.app/Contents/MacOS/blender"]

    for path in paths:
        if isexecutable(path):
            return path

    raise Exception("Blender not found, try setting the BLENDER_PATH environment variable")


class Light(collections.namedtuple('Light', ["location", "target", "size", "strength", "view_angle"])):
    def __new__(cls, location, target, size=2, strength=2, view_angle=45):
        return super(Light, cls).__new__(cls, location, target, size, strength, view_angle)


class Renderer():
    """Renderer.

    ^
    |    .-.
    |   |   | <- Head
    |   `^u^'
    Y |      ¦V <- Camera    (As seen from above)
    |      ¦
    |      ¦
    |      o <- Target

        ----------> X

    +X = left
    +Y = back
    +Z = up
    """

    _renderer_active = False

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        Renderer._renderer_active = False

    def __init__(self):
        Renderer._renderer_active = True

        self.eye_radius = 24/2
        self.eye_position = [0,0,0]
        self.eye_target = [0,-1000,0]
        self.eye_up = [0,0,1]
        self.eye_closedness = 0.0

        self.iris = "dark"

        self.cornea_refractive_index = 1.336

        self.pupil_radius = 4/2

        self.camera_position = None
        self.camera_target = None
        self.camera_up = [0,0,1]

        self.image_size = (640, 480)
        self.focal_length = (640/2.0) / math.tan(45*math.pi/180 / 2)
        self.focus_distance = None

        self.fstop = 2.0

        self.lights = []

        self.render_samples = 20

    def render(self, path, params=None, background=True, cuda=True):
        __, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext == ".png":
            render_format = "PNG"
        elif ext == ".jpg" or ext == ".jpeg":
            render_format = "JPEG"
        elif ext == ".bmp":
            render_format = "BMP"
        else:
            raise RuntimeError("Path extension needs to be one of png, jpg or bmp")

        new_ireye_path = os.path.join(TEXTURE_PATH, "ireye-{}.png".format(self.iris))
        if not os.path.exists(new_ireye_path):
            raise RuntimeError("Eye texture {} does not exist. Create one in the textures folder.".format("ireye-{}.png".format(self.iris)))

        with open(BLENDER_SCRIPT_TEMPLATE) as blender_script_template_file:
            blender_script_template = blender_script_template_file.read()
        blender_script_template = blender_script_template.replace("{","{{")
        blender_script_template = blender_script_template.replace("}","}}")
        blender_script_template = blender_script_template.replace("$INPUTS","{}")

        if self.camera_position is None:
            raise RuntimeError("Camera position not set")
        if self.camera_target is None:
            raise RuntimeError("Camera target not set")
        if len(self.lights) == 0:
            print("WARNING: No lights in scene")

        if self.focus_distance is None:
            focus_distance = math.sqrt(sum((a-b)**2 for a,b in zip(self.camera_position, self.camera_target)))
        else:
            focus_distance = self.focus_distance

        inputs = {
            "input_use_cuda": cuda,
            "input_eye_radius": self.eye_radius,
            "input_eye_pos": "Vector({})".format(list(self.eye_position)),
            "input_eye_target": "Vector({})".format(list(self.eye_target)),
            "input_eye_up": "Vector({})".format(list(self.eye_up)),
            "input_eye_closedness": self.eye_closedness,

            "input_iris": "'{}'".format(self.iris),

            "input_eye_cornea_refrative_index": self.cornea_refractive_index,

            "input_pupil_radius": self.pupil_radius,

            "input_cam_pos": "Vector({})".format(list(self.camera_position)),
            "input_cam_target": "Vector({})".format(list(self.camera_target)),
            "input_cam_up": "Vector({})".format(list(self.camera_up)),

            "input_cam_image_size": list(self.image_size),
            "input_cam_focal_length": self.focal_length,
            "input_cam_focus_distance": focus_distance,
            "input_cam_fstop": self.fstop,

            "input_lights": ["Light({})".format(
                    ",".join("{} = {}".format(k,v) for k,v in
                        {
                            "location": "Vector({})".format(list(l.location)),
                            "target": "Vector({})".format(list(l.target)),
                            "size": l.size,
                            "strength": l.strength,
                            "view_angle": l.view_angle
                        }.items())) for l in self.lights],

            "input_render_samples" : self.render_samples,
            "output_render_path" : "'{}'".format(path).replace("\\","/"),
        }
        if params:
            inputs["output_params_path"] = "'{}'".format(params).replace("\\","/")
        else:
            inputs["output_params_path"] = "None"

        def inputVal(v):
            if isinstance(v,list):
                return '[{}]'.format(",".join(inputVal(x) for x in v))
            else:
                return str(v)

        blender_script = blender_script_template.format("\n".join(
                            "{} = {}".format(k,inputVal(v)) for k,v in inputs.items()))
        blender_script = "\n".join("    " + x for x in blender_script.split("\n"))
        blender_script = ("import sys\ntry:\n" + blender_script +
            "\nexcept:"
            "\n    import traceback"
            "\n    with open('blender_err.log','a') as f:"
            "\n        f.write('\\n'.join(traceback.format_exception(*sys.exc_info())))"
            "\n    sys.exit(1)")

        with tempfile.NamedTemporaryFile("w+", suffix=".py", delete=False) as blender_script_file:
            blender_script_file.write(blender_script)

        try:
            with tempfile.NamedTemporaryFile(suffix="0000", delete=False) as blender_outfile:
                pass

            blender_args = [get_blender_path(),
                            MODEL_PATH,                              # Load model
                            "--enable-autoexec",                     # Automatic python script execution
                            "--verbose", "0",                        # No debug output
                            "--python", blender_script_file.name,    # Run the temporary blender script file
                            "-o", blender_outfile.name[:-4]+"####",  # Render output to temporary file
                            "--render-format", render_format,        # Set render format (e.g. Jpeg) 
                            "--use-extension", "0",]                 # Don't append the file extension
            if background:
                blender_args += ["--background"]                     # Load the file in the background (no UI)
                if self.render_samples > 0:
                    blender_args += ["--render-frame", "0"]          # Render frame 0

            # Write script to error log (
            with open("blender_err.log", "w") as blender_err_file:
                if sys.platform == 'win32':
                    blender_err_file.write(subprocess.list2cmdline(blender_args))
                else:
                    blender_err_file.write(" ".join('"{}"'.format(arg) if " " in arg else arg
                                                    for arg in blender_args))

                blender_err_file.write("\n\n")
                
                blender_err_file.write("{0}:\n".format(blender_script_file.name))
                blender_err_file.write("------\n")
                blender_err_file.write("\n".join(
                    "{: 4} | {}".format(i+1,x)
                    for i,x in enumerate(blender_script.split("\n"))))
                blender_err_file.write("\n------\n")

                while True:
                    try:
                        def enqueue_output(out, queue, name):
                            for line in iter(out.readline, b''):
                                queue.put((name, line.rstrip()))
                            out.close()

                        p = subprocess.Popen(blender_args, bufsize=0, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                        q = Queue()
                        tout = threading.Thread(target=enqueue_output, args=(p.stdout, q, "out"))
                        tout.daemon = True  # thread dies with the program
                        terr = threading.Thread(target=enqueue_output, args=(p.stderr, q, "err"))
                        terr.daemon = True  # thread dies with the program
                        tout.start()
                        terr.start()

                        print "Starting blender"

                        while tout.isAlive() or terr.isAlive():
                            try:
                                line = q.get(timeout=0.1)
                            except Empty:
                                pass
                            else:
                                if line[0] == "out":
                                    m = RENDER_LINE_RE.match(line[1])
                                    if m:
                                        tile = int(m.group("tile"))
                                        tiles = int(m.group("tiles"))
                                        sample = int(m.group("sample"))
                                        samples = int(m.group("samples"))
                                        print "Rendered {percent}%, time remaining: {rem} (tile {tile}/{tiles}, sample {sample}/{samples})".format(
                                            percent=100 * ((tile-1)*samples + (sample-1)) / (tiles*samples),
                                            **m.groupdict()
                                        )
                                
                                blender_err_file.write(line[1])
                                blender_err_file.write("\n")
                        
                        if p.wait() != 0:
                            print "Blender error"
                            raise subprocess.CalledProcessError(p.returncode, blender_args)

                        print "Blender quit"
                        break

                    except KeyboardInterrupt:
                        raise
                    except:
                        # Sometimes blender fails in rendering, so retry until success
                        traceback.print_exc()
                        print("Blender call failed, retrying in 1 sec")
                        time.sleep(1)

                if background and self.render_samples > 0:
                    if os.path.exists(path):
                        os.remove(path)
                    shutil.copy(blender_outfile.name, path)
                    print(("Moved image to {}".format(path)))

            # Remove error log if no errors occured
            os.remove("blender_err.log")

        except KeyboardInterrupt:
            raise

        except:
            if os.path.exists("blender_err.log"):
                with open("blender_err.log") as blender_err_file:
                    print(blender_err_file.read())

            raise
        finally:
            os.remove(blender_script_file.name)
            #os.remove(blender_outfile.name)
