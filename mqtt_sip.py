import threading
import subprocess
import time
import json
import os
import queue
import re
from gtts import gTTS
import paho.mqtt.client as mqtt

# MQTT-Server Konfiguration
MQTT_BROKER = "192.168.2.1"
MQTT_PORT = 1883
MQTT_TOPIC_STATUS = "ttsip/s/status"
MQTT_TOPIC_CALL = "ttsip/r/call"
MQTT_CLIENT_ID = "ttsip"
MQTT_USERNAME = "ha"
MQTT_PASSWORD = "ah"

# Nachrichtenwarteschlange erstellen
mqtt_message_queue = queue.Queue()
bareisp_message_out_queue = queue.Queue()

# MQTT-Client Initialisierung
client = mqtt.Client(MQTT_CLIENT_ID)
client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

def on_connect(client, userdata, flags, rc):
	print(f"Connected to MQTT Broker with result code {rc}")
	client.subscribe(MQTT_TOPIC_CALL)
	
def on_disconnect(client, userdata, rc):
    """Callback-Funktion für MQTT-Disconnects."""
    if rc != 0:
        print("Unexpected disconnection. Reconnecting...")
    else:
        print("Disconnected from MQTT Broker.")

    # Manuelles Wiederverbinden versuchen
    while True:
        try:
            client.reconnect()
            print("Reconnected to MQTT Broker.")
            client.publish(MQTT_TOPIC_STATUS, "ready")
            break
        except Exception as e:
            print(f"Reconnect failed: {e}. Retrying in 5 seconds...")
            time.sleep(5)

def is_valid_sip_number(sip_number):
	"""Prüft, ob die SIP-Nummer gültig ist."""
	return re.fullmatch(r'[0+]\d*', sip_number) is not None

def on_message(client, userdata, msg):
	print(f"Message received on {msg.topic}: {msg.payload.decode()}")
	if(msg.topic == MQTT_TOPIC_CALL):
		print("call topic")
		payload = json.loads(msg.payload.decode())
		sip_number = payload.get("sip", "")
		message = payload.get("msg", "")
		if sip_number and message:
			if is_valid_sip_number(sip_number):
				print("calling")
				# Nachricht in die Queue legen
				mqtt_message_queue.put((sip_number, message))
			else:
				print(f"Invalid SIP number: {sip_number}")

def generate_tts(message, output_path):
	tts = gTTS(text=message, lang='en')
	temp_mp3 = '/tmp/temp_message.mp3'
	tts.save(temp_mp3)
	os.remove(output_path)
	subprocess.run([
		'ffmpeg', '-i', temp_mp3, '-ac', '1', '-ar', '8000', '-acodec', 'pcm_s16le', output_path
	], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	os.remove(temp_mp3)

def process_queue(process):
	while not mqtt_message_queue.empty():
		sip_number, message = mqtt_message_queue.get()
		print(f"Processing call to {sip_number} with message: {message}")
		client.publish(MQTT_TOPIC_STATUS, "connecting")
		
		# second: start baresip and start thread for supervision
		process = subprocess.Popen(['baresip','-f','/root/.baresip'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
		output_thread = threading.Thread(target=monitor_baresip_output, args=(process,))
		output_thread.daemon = True  # Beendet den Thread, wenn das Hauptprogramm beendet wird
		output_thread.start()
		
		print("Wating for baresip to connect ", end="")
		while True:
			if not bareisp_message_out_queue.empty():
				output = bareisp_message_out_queue.get()
				if "1 binding" in output:
					print("Registration successful!")
					client.publish(MQTT_TOPIC_STATUS, "dailing")
					break
			else:
				print(".", end="")
				time.sleep(0.5)

		# Nummer wählen
		process.stdin.write(f"d {sip_number}\n".encode())
		process.stdin.flush()

		# Aufruf bestätigen
		while True:
			if not bareisp_message_out_queue.empty():
				output = bareisp_message_out_queue.get()
				if "Call established" in output:
					print("Call established!")
					break
#				else:
#					print("BareSIP: " + output.decode(), end="")
			else:
				time.sleep(0.1)

		# TTS-Nachricht generieren
		print("Generating TTS message...")
		client.publish(MQTT_TOPIC_STATUS, "generating")
		output_wav = '/tmp/alert_message.wav'
		generate_tts(message, output_wav)
		time.sleep(1) # extra time for the remote end to get the phone to the ear

		# TTS-Nachricht abspielen
		print("Playing TTS message...")
		client.publish(MQTT_TOPIC_STATUS, "talking")
		process.stdin.write(f"/ausrc aufile,{output_wav}\n".encode())
		process.stdin.flush()

		# Warten auf Ende des Anrufs
		while True:
			if not bareisp_message_out_queue.empty():
				output = bareisp_message_out_queue.get()
				if "terminated" in output:
					print("Call terminated.")
					break
#				else:
#					print(output.decode(), end="")
			else:
				time.sleep(0.1)
		
		output_thread.stop()
		# return to ready state
		client.publish(MQTT_TOPIC_STATUS, "ready")

def monitor_baresip_output(process):
	"""Kontinuierliche Überwachung des BareSIP-Outputs."""
	while True:
		try:
			output = process.stdout.readline()
			if output:
				print("[BareSIP] " + output.decode(), end="")
				bareisp_message_out_queue.put(output.decode())
		except Exception as e:
			print(f"Error reading BareSIP output: {e}")
			break


def run_baresip_and_mqtt():
	# first: connect MQTT 
	client.on_connect = on_connect
	client.on_message = on_message
	client.on_disconnect = on_disconnect  # Disconnect-Callback hinzufügen

	try:
		client.connect(MQTT_BROKER, MQTT_PORT)
		client.loop_start()
		client.publish(MQTT_TOPIC_STATUS, "starting")
	except Exception as e:
		print(f"Failed to connect to MQTT Broker: {e}")
		return
		
	client.publish(MQTT_TOPIC_STATUS, "starting")

	# Separate Schleife für BareSIP-Output
	while True:
		process_queue(process)
		time.sleep(0.1)
		

if __name__ == "__main__":
	run_baresip_and_mqtt()
