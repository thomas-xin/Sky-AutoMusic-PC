import concurrent.futures
import json
import os
import subprocess
import threading
import time
import keyboard
import pydirectinput
pydirectinput.PAUSE = 0
import pygetwindow as gw
import chardet
import orjson

SUPPORTED = {"txt", "json", "skysheet"}

try:
	s = subprocess.check_output(["hyperchoron", "-lf"], encoding="utf-8")
except (FileNotFoundError, subprocess.CalledProcessError):
	has_hyperchoron = False
else:
	has_hyperchoron = True
	_, encoders = s.split("# Encoders:\n", 1)
	encoders, decoders = encoders.split("\n# Decoders:\n", 1)
	SUPPORTED.update(decoders.splitlines())


def json_default(obj):
	if isinstance(obj, datetime.datetime):
		return obj.strftime("%Y-%m-%dT%H:%M:%S")
	if isinstance(obj, np.number):
		return obj.item()
	if isinstance(obj, (set, frozenset, alist, deque, np.ndarray)):
		return list(obj)
	raise TypeError(obj)

class MultiEncoder(json.JSONEncoder):

	def default(self, obj):
		return json_default(obj)

def json_dumps(obj, *args, **kwargs):
	return orjson.dumps(obj, *args, default=json_default, **kwargs)
def json_dumpstr(obj, *args, **kwargs):
	return orjson.dumps(obj, *args, default=json_default, **kwargs).decode("utf-8", "replace")

class PrettyJSONEncoder(json.JSONEncoder):

	def __init__(self, *args, **kwargs):
		self.indent = kwargs.get("indent") or "\t"
		super().__init__(*args, **kwargs)

	def encode(self, obj, level=0):
		indent = " " * self.indent if type(self.indent) is int else self.indent
		curr_indent = indent * level
		next_indent = indent * (level + 1)
		if isinstance(obj, (list, tuple)):
			if all(not isinstance(x, (tuple, list, dict)) or len(json_dumps(x)) < 10 for x in obj):
				return "[" + ", ".join(json_dumpstr(x) for x in obj) + "]"
			items = [self.encode(x, level=level + 1) for x in obj]
			return "[\n" + next_indent + f",\n{next_indent}".join(item for item in items) + f"\n{curr_indent}" + "]"
		elif isinstance(obj, dict):
			if all(type(x) is str and len(x) <= max(10, len(obj)) for x in obj.values()) and all(type(x) is str and len(x) <= max(10, len(obj)) for x in obj.keys()):
				return json.dumps(obj)
			items = [f"{json_dumpstr(k)}: {self.encode(v, level=level + 1)}" for k, v in obj.items()]
			items.sort()
			return "{\n" + next_indent + f",\n{next_indent}".join(item for item in items) + f"\n{curr_indent}" + "}"
		return json_dumpstr(obj)

	def default(self, obj):
		return json_default(obj)

prettyjsonencoder = PrettyJSONEncoder(indent="\t")
pretty_json = lambda obj: prettyjsonencoder.encode(obj)


def read_json_file(file_path, try_hyperchoron=True):
	with open(file_path, "rb") as f:
		b = f.read()
	try:
		return orjson.loads(b)
	except orjson.JSONDecodeError:
		if has_hyperchoron and try_hyperchoron:
			if not os.path.exists(file_path + "~") or not os.path.getsize(file_path + "~"):
				try:
					subprocess.run(["hyperchoron", "-i", file_path, "-si", "-f", "skysheet", "-o", file_path + "~"])
				except FileNotFoundError:
					raise ValueError(f"Invalid JSON file: {file_path}. If this was a MIDI file, please check out https://github.com/thomas-xin/hyperchoron for conversion!")
			return read_json_file(file_path + "~", try_hyperchoron=False)
		raise ValueError(f"Invalid JSON file: {file_path}.")

def load_song(file_path):
	data = read_json_file(file_path)
	if isinstance(data, dict):
		data = [data]
	return data

def save_song(data, file_path):
	s = pretty_json(data)
	with open(file_path, "w", encoding="utf-8") as f:
		f.write(s)
	return file_path

def produce_songnotes(song):
	if not song.get("columns"):
		return
	output = []
	bpm_ms = 60000 / song["bpm"]
	timestamp = 100
	for column in song["columns"]:
		tempo = 1 / 2 ** column[0]
		for note in column[1]:
			layers = int(note[1], 16)
			count = sum(map(int, bin(layers)[2:]))
			output.append(dict(
				key=f"{count}Key{note[0]}",
				time=round(timestamp),
			))
		timestamp += bpm_ms * tempo
	song["songNotes"] = output


class MusicHandler:
	exitProgram = False
	pauseProgram = False
	data = None
	config = None

	def __init__(self, file_path, config):
		self.started = threading.Condition()
		self.file_path = file_path
		self.config = config
		self.data = load_song(file_path)
		if self.data[0].get("columns"):
			produce_songnotes(self.data[0])
		self.curr_note = 0
		self.max_note = 1
		self.start_key, self.stop_key = self.get_hotkeys()
		keyboard.add_hotkey(self.stop_key, lambda: self.pause())
		self.running = threading.Thread(target=self.run, daemon=True)
		self.running.start()

	def run(self):
		notes = self.data[0]['songNotes']
		threading.Thread(target=self.wait_start).start()
		while not self.exitProgram:
			with self.started:
				self.started.wait()
			if self.exitProgram:
				break
			self.pauseProgram = False
			if gw.getActiveWindowTitle().split(None, 1)[0] == 'Sky':
				self.simulate_keyboard_presses(notes)

	def wait_start(self):
		try:
			keyboard.wait(self.start_key)
		except KeyError:
			pass
		with self.started:
			self.started.notify_all()

	def get_hotkeys(self):
		keys = self.config.read_config()["music"]
		return keys["start_key"]["scan_code"], keys["stop_key"]["scan_code"]

	def is_alive(self):
		return not self.exitProgram

	def quit(self):
		self.exitProgram = True
		with self.started:
			self.started.notify_all()
		self.running.join()

	def pause(self):
		self.pauseProgram = True

	def simulate_keyboard_presses(self, notes):
		if self.exitProgram:
			return
		print("Starting playback...")
		key_mapping = self.config.read_config()["music"]["key_mapping"]

		notes_dict = {}
		for note in notes:
			t = note["time"]
			t /= 1000
			if t in notes_dict:
				notes_dict[t].add(key_mapping.get(note['key'][4:]))
			else:
				notes_dict[t] = set([key_mapping.get(note['key'][4:])])
		notes = [(k, "".join(v)) for k, v in notes_dict.items()]
		if notes:
			start_time = time.time() - notes[0][0] + 0.25

			self.max_note = round(max(notes_dict))
			self.curr_note = 0

			def press(k):
				pydirectinput.keyUp(k)
				return pydirectinput.keyDown(k)
			def callback(k):
				time.sleep(0.04)
				return pydirectinput.keyUp(k)

			with concurrent.futures.ThreadPoolExecutor(max_workers=64) as exc:
				for note in notes:
					next_time = note[0] + start_time
					delay = next_time - time.time()
					if delay > 0:
						time.sleep(delay)

					if self.pauseProgram or self.exitProgram:
						break

					print(note)
					key_to_press = note[1]
					for k in key_to_press:
						press(k)
						exc.submit(callback, k)
					self.curr_note = round(note[0])
		self.exitProgram = True

def mstart(file, config):
	return MusicHandler(file, config)