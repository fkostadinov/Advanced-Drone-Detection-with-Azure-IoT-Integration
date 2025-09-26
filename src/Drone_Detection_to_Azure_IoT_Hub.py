import cv2
import torch
import numpy as np
from PIL import Image
import json
import time
import asyncio
import threading
import os
from pathlib import Path 
from dotenv import load_dotenv

# Import the Azure IoT Hub Device Client and exceptions
from azure.iot.device.aio import IoTHubDeviceClient
from azure.iot.device import Message, exceptions
from azure.iot.device.exceptions import OperationTimeout # Use the correct exception class

# Import SSL for unverified context (if needed)
import ssl


# Suppress unnecessary PyTorch warnings
import warnings
warnings.simplefilter('ignore', FutureWarning)

# --- Locate and Load .env File ---
# Get the directory where the current script file is located (the 'src' folder).
BASE_DIR = Path(__file__).resolve().parent
# Construct the full path to the .env file (assuming .env is in the same folder as the script).
DOTENV_PATH = BASE_DIR / '.env'

# Load environment variables from the specified path.
load_dotenv(dotenv_path=DOTENV_PATH)

# --- IoT Hub Configuration ---
# Read the Connection String from the environment variable (or hardcode if necessary)
CONNECTION_STRING = os.getenv("AZURE_IOT_HUB_CONNECTION_STRING")

# --- WARNING/PLACEHOLDER CHECK ---
if not CONNECTION_STRING or CONNECTION_STRING == "PLACEHOLDER_ERROR_STRING":
    print("🔴 FATAL: AZURE_IOT_HUB_CONNECTION_STRING is not set or is invalid.")
    print("Please check your .env file or hard-code the value.")
    CONNECTION_STRING = "PLACEHOLDER_ERROR_STRING" 
# --- End IoT Hub Configuration ---


# Don't do this in production... (Suppressing SSL verification warning)
ssl._create_default_https_context = ssl._create_unverified_context


# Check if MPS is available to make use of Apple Silicon
if torch.backends.mps.is_available():
    print("MPS is available! Moving torch to MPS.")
    device = torch.device("mps")
else:
    print("MPS is not available! Using CPU.")
    device = torch.device("cpu")


# Load YOLOv5 model
try:
    model = torch.hub.load('ultralytics/yolov5', 'custom', path='src/best.pt', source='github')
except Exception as e:
    print(f"Error loading YOLOv5 model: {e}")
    print("Ensure 'src/best.pt' is available or check your path.")
    exit()

# Move model to the device
model = model.to(device)

# Set video source (webcam or video file)
cap = cv2.VideoCapture(0)

# Define the classes you want to detect
classes = ['Drone']

# --- Detection Confirmation Variables (Main Thread State) ---
detection_state = False           # True if a confirmed detection is currently active
detection_start_time = 0.0        # Time when detection was first observed
CONFIRMATION_DURATION = 0.5       # Minimum duration (in seconds) required for confirmation
# --- End Detection Confirmation Variables ---


# --- Threading/Synchronization Setup (Shared State) ---
shared_data = {
    'coords': [(50, 50), (250, 50), (250, 250), (50, 250)],
    'run_flag': True,
    'detection_confirmed': False  # State communicated to the IoT thread
}
coords_lock = threading.Lock() # Lock for thread-safe access

rectangle_drag = False
drag_corner = -1

# Function to handle mouse events
def mouse_event(event, x, y, flags, param):
    global rectangle_drag, drag_corner

    with coords_lock:
        coords = shared_data['coords']
        
        if event == cv2.EVENT_LBUTTONDOWN:
            for i, corner in enumerate(coords):
                if abs(corner[0] - x) <= 10 and abs(corner[1] - y) <= 10:
                    rectangle_drag = True
                    drag_corner = i
                    break

        elif event == cv2.EVENT_LBUTTONUP:
            rectangle_drag = False

        elif event == cv2.EVENT_MOUSEMOVE:
            if rectangle_drag:
                coords[drag_corner] = (x, y)


# --- Azure IoT Async Sender Function ---

async def send_rectangle_coords_loop(client):
    """
    An asynchronous function that periodically checks the state and sends
    coordinates to Azure IoT Hub if a CONFIRMED threat is present.
    """
    if client is None: # Safety check (shouldn't be needed here, but safe)
        print("🔴 IoT client is None in sender loop. Exiting.")
        return

    SEND_INTERVAL_SECONDS = 3.0
    print(f"✅ IoT Hub sender starting, checking state every {SEND_INTERVAL_SECONDS} seconds...")

    try:
        print("ATTEMPTING CONNECT...")
        await client.connect()
        print("✅ CONNECTED to IoT Hub successfully.")
    except OperationTimeout: 
        print("❌ CRITICAL: Could not connect to IoT Hub (Timeout). Check network and firewall.")
        return
    except Exception as e:
        print(f"❌ CRITICAL: Failed to connect to IoT Hub: {e}")
        return

    while shared_data['run_flag']:
        try:
            with coords_lock:
                is_confirmed = shared_data['detection_confirmed']
                current_coords = list(shared_data['coords'])

            if is_confirmed:
                # Format payload...
                json_coords = [
                    {"corner": i, "x": c[0], "y": c[1]}
                    for i, c in enumerate(current_coords)
                ]
                payload = {
                    "timestamp": time.time(),
                    "alert_status": "CONFIRMED_THREAT",
                    "rectangle_coordinates": json.dumps(json_coords) # Double-check payload structure
                }

                # CRITICAL DEBUG: Print right before send
                print(f"ATTEMPTING SEND: {time.strftime('%H:%M:%S')} (Confirmed: {is_confirmed})")

                message = Message(json.dumps(payload))
                await client.send_message(message)
                
                # CRITICAL DEBUG: Confirmation print
                print(f"✅ SENT IoT: Confirmed threat coordinates successfully.")
            else:
                # Print periodic status when no threat is confirmed
                print(f"STATUS: {time.strftime('%H:%M:%S')} - No confirmed threat. (Skipping send)")

        except exceptions.ClientError as e:
            print(f"❌ Azure IoT Client Error during send: {e}")
        except Exception as e:
            print(f"❌ Unexpected error in sender loop during send: {e}")

        await asyncio.sleep(SEND_INTERVAL_SECONDS)

    try:
        await client.disconnect()
        print("✅ IoT Hub client disconnected.")
    except Exception as e:
        print(f"❌ Error during disconnect: {e}")
        
    
    """
    An asynchronous function that periodically checks the state and sends
    coordinates to Azure IoT Hub if a CONFIRMED threat is present.
    """
    if CONNECTION_STRING == "PLACEHOLDER_ERROR_STRING":
        print("🔴 IoT Hub sender deactivated: CONNECTION_STRING was not read.")
        return

    SEND_INTERVAL_SECONDS = 3.0
    print(f"✅ IoT Hub sender starting, checking state every {SEND_INTERVAL_SECONDS} seconds...")

    try:
        await client.connect()
    except OperationTimeout: 
        print("❌ Could not connect to IoT Hub (Timeout). Check your network.")
        return
    except Exception as e:
        print(f"❌ Failed to connect to IoT Hub: {e}")
        return

    while shared_data['run_flag']:
        try:
            # 1. Acquire the lock and read the latest state and coordinates
            with coords_lock:
                is_confirmed = shared_data['detection_confirmed']
                current_coords = list(shared_data['coords'])

            if is_confirmed:
                # 2. Format the data for JSON
                json_coords = [
                    {"corner": i, "x": c[0], "y": c[1]}
                    for i, c in enumerate(current_coords)
                ]

                payload = {
                    "timestamp": time.time(),
                    "alert_status": "CONFIRMED_THREAT",
                    "rectangle_coordinates": json_coords
                }

                # 3. Create and send the message
                message = Message(json.dumps(payload))
                await client.send_message(message)
                print(f"SENT IoT: Confirmed threat coordinates.")
            else:
                pass 

        except exceptions.ClientError as e:
            print(f"❌ Azure IoT Client Error: {e}")
        except Exception as e:
            print(f"❌ Unexpected error in sender loop: {e}")

        # 4. Wait for the specified interval
        await asyncio.sleep(SEND_INTERVAL_SECONDS)

    try:
        await client.disconnect()
        print("✅ IoT Hub client disconnected.")
    except Exception as e:
        print(f"❌ Error during disconnect: {e}")


def iot_thread_target(client):
    """Target function for the dedicated IoT sender thread."""
    # Since the thread is only started if client is NOT None, this function
    # is guaranteed a valid client object.
    try:
        asyncio.run(send_rectangle_coords_loop(client))
    except RuntimeError as e:
        if 'cannot set a new event loop' in str(e):
            pass 
        else:
            raise


# --- Main Execution ---

# --- Main Execution ---

# 1. Initialize variables globally so they are guaranteed to exist, even if None.
# This prevents Python scoping errors in the cleanup block.
iot_client = None
iot_thread = None

try:
    # This line attempts to create the client. It raises ValueError if malformed.
    iot_client = IoTHubDeviceClient.create_from_connection_string(CONNECTION_STRING)
    print("✅ IoT Hub client created successfully.")
except ValueError as e:
    print(f"❌ Failed to create IoT Hub client (Check connection string format): {e}")
    # iot_client remains None
except Exception as e:
    print(f"❌ An unexpected error occurred during client creation: {e}")
    # iot_client remains None


# 2. Start the IoT Sender Thread ONLY if the client object is valid
if iot_client is not None:
    print("✅ Starting sender thread.")
    iot_thread = threading.Thread(target=iot_thread_target, args=(iot_client,))
    iot_thread.daemon = True 
    iot_thread.start()
else:
    # If the client is None, the system correctly reports failure and proceeds without the thread.
    print("🔴 Skipping IoT thread creation due to client initialization failure.")


# 3. Create OpenCV window and set the mouse event callback function
cv2.namedWindow('frame')
cv2.setMouseCallback('frame', mouse_event)

while True:
    # Read frame from video source
    ret, frame = cap.read()
    if not ret:
        print("Can't receive frame (stream end?). Exiting ...")
        break

    current_frame_time = time.time()
    drone_detected_in_area_this_frame = False

    # Convert the frame to a format that YOLOv5 can process
    img = Image.fromarray(frame[...,::-1])

    # Run inference on the frame
    results = model(img, size=640)

    # Process the results and draw bounding boxes on the frame
    with coords_lock:
        current_coords = shared_data['coords']
        
        # Calculate AABB for the restricted area once per frame
        rx_min = min(c[0] for c in current_coords)
        rx_max = max(c[0] for c in current_coords)
        ry_min = min(c[1] for c in current_coords)
        ry_max = max(c[1] for c in current_coords)
        
        for result in results.xyxy[0]:
            x1, y1, x2, y2, conf, cls = result.tolist()
            
            if conf > 0.5 and classes[int(cls)] in classes:
                # Draw the bounding box
                cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)
                
                # Display confidence and coordinates
                text_conf = "{:.2f}%".format(conf * 100)
                cv2.putText(frame, text_conf, (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

                # Check if the drone intersects with the rectangle (AABB check)
                drone_in_area = (x1 < rx_max and x2 > rx_min and y1 < ry_max and y2 > ry_min)

                if drone_in_area:
                    drone_detected_in_area_this_frame = True
                    # Display immediate warning message
                    cv2.putText(frame, "Warning: Drone Detected Under Restricted Area!", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        # Draw the rectangle and its corners
        for i in range(4):
            cv2.circle(frame, current_coords[i], 5, (0, 255, 0), -1)
            cv2.line(frame, current_coords[i], current_coords[(i+1)%4], (0, 255, 0), 2)

    # --- Confirmation State Machine Logic ---
    if drone_detected_in_area_this_frame:
        if not detection_state:
            # Start timing if not already timed
            if detection_start_time == 0.0:
                detection_start_time = current_frame_time
            
            # Check if the required time has passed
            if (current_frame_time - detection_start_time) >= CONFIRMATION_DURATION:
                # CONFIRMED DETECTION
                detection_state = True
                print(f"CONFIRMED: Drone detected for >{CONFIRMATION_DURATION}s.")
                
                # Update shared state for the IoT thread
                with coords_lock:
                    shared_data['detection_confirmed'] = True 
                
        if detection_state:
            # Draw a CONFIRMED ALERT text
            cv2.putText(frame, "ALERT: CONFIRMED THREAT!", (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    else:
        # No drone detected in the restricted area this frame
        if detection_state:
            # State transition: Confirmed detection ended
            detection_state = False
            detection_start_time = 0.0
            print("CLEAR: Confirmed detection ended.")
            
            # Update shared state for the IoT thread
            with coords_lock:
                shared_data['detection_confirmed'] = False 
                
        elif detection_start_time != 0.0:
            # Reset the initial observation time if the detection was transient
            detection_start_time = 0.0
            
    # --- End Confirmation State Machine Logic ---


    # Display the resulting frame
    cv2.imshow('frame', frame)

    # Wait for key press to exit
    if cv2.waitKey(1) == ord('q'):
        break

# --- Cleanup ---
print("\nExiting application...")
# Signal the IoT thread to stop its loop
shared_data['run_flag'] = False 

# Check if the thread was ever created and is running before joining
if iot_thread and iot_thread.is_alive():
    # Wait for the thread to finish gracefully
    iot_thread.join(timeout=5) 

# Release the video source and close the window
cap.release()
cv2.destroyAllWindows()