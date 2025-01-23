import platform
import can
import math
import time
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit
import threading
from datetime import datetime
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import os

app = Flask(__name__)
socketio = SocketIO(app)

# Suppress Flask's default logs
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Determine the operating system
IS_MAC = platform.system() == "Darwin"

# Global settings
SETTINGS_FILE = "settings.json"
latest_bearing = None
last_data_time = None
frequency = 1  # Default frequency in Hz
SID = 0  # Initial Sequence ID
CAN_INTERFACE = "can0"  # Replace with your CAN interface name (e.g., "can0", "vcan0")
bus = None  # Global CAN bus object


def log(message, category="log"):
    """Log a message to the console and emit it to the front-end."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    try:
        socketio.emit(category, {"message": full_message})
    except Exception as e:
        print(f"Error emitting log: {e}")


def save_settings(frequency):
    """Save the current frequency setting to a file."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"frequency": frequency}, f)
        log(f"Frequency {frequency} Hz saved to {SETTINGS_FILE}.")
    except Exception as e:
        log(f"Error saving settings: {e}")


def load_settings():
    """Load the frequency setting from a file."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
        log(f"Loaded settings: {settings}")
        return settings.get("frequency", 1)  # Default to 1 Hz if not found
    except FileNotFoundError:
        log("Settings file not found. Using default settings.")
        return 1  # Default frequency
    except Exception as e:
        log(f"Error loading settings: {e}")
        return 1  # Default frequency


class FileChangeHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(".html"):
            log(f"File changed: {event.src_path}. Triggering reload.")
            try:
                socketio.emit("reload")
            except Exception as e:
                log(f"Error emitting reload event: {e}")


def start_file_observer():
    """Start observing the templates directory for changes."""
    observer = Observer()
    handler = FileChangeHandler()
    observer.schedule(handler, path=os.path.join(os.getcwd(), "templates"), recursive=False)
    observer.start()
    log("File observer started.")
    return observer


@app.route("/listen", methods=["POST"])
def listen():
    """Receive magnetic bearing data via REST."""
    global latest_bearing, last_data_time

    data = request.get_json()
    log(f"Received data: {data}", category="incoming_message")

    if data and "payload" in data:
        for entry in data["payload"]:
            if "values" in entry and "magneticBearing" in entry["values"]:
                latest_bearing = entry["values"]["magneticBearing"]
                last_data_time = time.time()
                log(f"Updated magneticBearing to: {latest_bearing}", category="incoming_message")

    return jsonify({"status": "received"}), 200


@app.route("/")
def index():
    """Render the live logs web page."""
    return render_template("index.html")


@socketio.on("update_frequency")
def update_frequency(data):
    """Update the frequency dynamically from the web UI and save it."""
    global frequency
    frequency = max(1, min(10, data.get("frequency", 1)))
    save_settings(frequency)
    log(f"Frequency updated to: {frequency} Hz")


@socketio.on("get_frequency")
def get_frequency():
    """Send the current frequency to the client."""
    socketio.emit("current_frequency", {"frequency": frequency})
    log(f"Sent current frequency: {frequency} Hz to client.")


def prepare_nmea_message(heading, sid):
    """Prepare a valid NMEA 2000 PGN 127250 (Vessel Heading) message."""
    # Normalize heading to the range 0 to 360 degrees
    heading = heading % 360

    # Convert heading to radians and scale to centi-radians
    heading_radians = math.radians(heading)
    heading_encoded = int(heading_radians * 10000) & 0xFFFF  # Scale to centi-radians

    sid_byte = sid & 0xFF  # Ensure SID is 1 byte
    deviation = 0xFFFF  # Deviation unavailable
    variation = 0xFFFF  # Variation unavailable
    reference = 0b00  # Magnetic reference

    # Pack data fields into bytes
    data = [
        sid_byte,
        heading_encoded & 0xFF,  # Heading LSB
        (heading_encoded >> 8) & 0xFF,  # Heading MSB
        deviation & 0xFF,  # Deviation LSB
        (deviation >> 8) & 0xFF,  # Deviation MSB
        variation & 0xFF,  # Variation LSB
        (variation >> 8) & 0xFF,  # Variation MSB
        reference,  # Reference type
    ]

    return data, heading, heading_radians


def send_nmea2000_can_message(data):
    """Send the NMEA 2000 PGN 127250 message via CAN."""
    global bus
    pgn = 127250
    priority = 2
    source_address = 0x01  # Fixed source address for the script

    # Calculate 29-bit CAN ID for NMEA 2000
    can_id = (
        (priority << 26)  # Priority (3 bits)
        | (pgn << 8)      # PGN (18 bits)
        | source_address  # Source address (8 bits)
    )

    # Create the CAN message
    message = can.Message(
        arbitration_id=can_id,
        data=data,
        is_extended_id=True,  # NMEA 2000 uses 29-bit extended IDs
    )

    try:
        if IS_MAC:
            log(f"[Simulated] Sending CAN message: {message}", category="nmea_message")
        else:
            bus.send(message)
            log(f"Sent NMEA message: PGN=127250, Data={data}", category="nmea_message")
    except can.CanError as e:
        log(f"Failed to send CAN message: {e}")


def process_data():
    """Send the latest magnetic bearing at the configured frequency."""
    global latest_bearing, last_data_time, SID, bus, frequency

    try:
        log("Started process_data thread.")

        while True:
            current_time = time.time()

            if (
                latest_bearing is not None
                and last_data_time is not None
                and (current_time - last_data_time) <= (1 / frequency)
            ):
                # Prepare and send the NMEA 2000 message
                data, heading_degrees, heading_radians = prepare_nmea_message(latest_bearing, SID)
                send_nmea2000_can_message(data)
                log(
                    f"Sent CAN message: {data} "
                    f"(Heading: {heading_degrees:.2f}° / {heading_radians:.4f} radians)",
                    category="nmea_message",
                )
                SID = (SID + 1) % 256  # Wrap SID at 255
            else:
                # Log reasons for no data
                if latest_bearing is None:
                    log("No data received: latest_bearing is None.")
                elif last_data_time is None:
                    log("No data received: last_data_time is None.")
                elif (current_time - last_data_time) > (1 / frequency):
                    log("No data received: Data is outdated.")
                else:
                    log("No data received: Unknown condition.")

            time.sleep(1 / frequency)
    except Exception as e:
        log(f"Error in process_data: {e}")


if __name__ == "__main__":
    log("Loading settings...")
    frequency = load_settings()

    log("Starting file observer...")
    observer = start_file_observer()

    log("Starting process_data thread...")
    threading.Thread(target=process_data, daemon=True).start()

    log("Starting Flask-SocketIO server...")
    try:
        socketio.run(app, host="0.0.0.0", port=6969, debug=True)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()