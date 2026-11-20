# SENTRIX submission video: narration

One video for the team, 7 min 59 s. 0:00-0:20 is the title card (the five students and their roll numbers, the project, its title, the university), shown from the first frame. The narrator is Kokoro's 'Michael' voice; the team's roles are in Kartik Garg's own voice.

**0:00 · Title** — Sentrix: intelligent multimodal edge threat intelligence and physical security system. A capstone project by Kartik Garg, Akshay Ranveer, Prashant Gagneja, Harshit Mishra and Mehul Perimal, Thapar Institute of Engineering and Technology, guided by Doctor Ashutosh Mishra.

**0:20 · problem**  
Home alarms raise far more false alarms than real ones: police studies put them at 94 to 98 percent. And cloud cameras send private video out of the home.

**0:32 · hero**  
SENTRIX is our answer. One small unit watches a room, and decides on the device itself whether there is a handgun or a visible flame. It asks the owner before anyone else is alerted, and its video, faces and evidence stay on the device.

**0:50 · team**  
Kartik led vision and the backend: turning detector scores into alarms we can trust, and the architecture of the whole system. Akshay led identity: recognising residents per tracked person, body re-identification, and setting the match bars from error rates we measured. Prashant led audio: capturing sound, tagging it with an AudioSet model, and aligning every sound window with the video. Harshit led behaviour and fusion: the data-collection protocol, and the transparent rules that combine every input into one alarm level. Mehul led records, interfaces and evaluation: tamper-evident evidence storage, the alarm screens, and judging the product by the alarm level a user actually sees.

**1:43 · agenda**  
This video follows the six parts of the guideline: the components, the connections, the inputs, how to start, stop and troubleshoot it, a complete run, and the final results.

**1:55 · programs**  
SENTRIX has two programs. The device runs on a Raspberry Pi 5: it owns the camera, runs every model, and serves its own screens. The Portal runs in the cloud: it holds accounts, contacts and a responder desk, and reaches the device only through the device's own outbound link.

**2:15 · modules**  
Inside the device, perception models produce votes: a YOLO person, weapon and fire detector, a DINOv2 scene classifier trained on our own footage, face recognition, body re-identification and a sound tagger. Evidence rules confirm the votes, the threat confidence index and the level policy commit one of five levels, and the SOS engine asks the owner. Every message passes one send guard, and test mode is on by default.

**2:46 · physical**  
Our unit is a Raspberry Pi 5 with 8 GB of memory and an active cooler, powered by the official 27-watt USB-C supply. A Camera Module 3 sits on a tilting mount, a USB microphone listens, and three status LEDs show its state, all in our 3D-printed case.

**3:08 · wiring**  
The camera connects to the Pi's camera port with a ribbon cable, and the microphone goes into any USB port, even while SENTRIX is running. The green, amber and red LEDs are on GPIO 5, 12 and 13, through 330-ohm resistors. The official 27-watt USB-C supply powers the Pi. The README and the Pi guide in our repository list every pin.

**3:36 · inputs**  
SENTRIX needs a camera picture; sound from the USB microphone is optional. To set it up, the owner activates an account with a one-time code, and enters the home's address, the household, emergency contacts, who confirm by consent, and a PIN and a duress PIN. Residents' faces are enrolled from the device's own camera, so the people who live there are not treated as strangers.

**4:03 · start**  
On the Pi, SENTRIX is a service that starts by itself at boot, and the status command shows it running. The Portal is hosted on Render and always on, so there is nothing to start: the owner simply opens it in a browser.

**4:18 · stop**  
The same service commands stop or restart it. The event log records every level change and every incident, and the doctor tool checks the models, the settings, the send guard and the evidence chain, without opening the camera.

**4:34 · trouble**  
Most problems have a short fix. No picture: reseat the camera ribbon. No sound: plug the microphone into any USB port; it is picked up automatically. Device shown offline: check the Wi-Fi; the link reconnects on its own. And if a room gives false alarms, the calibration tool sets that room's thresholds from the device's own log.

**4:57 · run**  
Now a complete run, in test mode, so nothing can be sent anywhere. A held-out clip with a replica handgun, staged by our team, plays as the camera.

**5:08 · confirm**  
The scene model and the weapon detector both confirm the handgun, with a person in view. Level 3 is committed, and the device screen switches to the alert.

**5:22 · owner**  
On the owner's phone a red bar appears, and the alert puts the three answers first. The owner chooses Police and asks for help.

**5:39 · desk**  
The SENTRIX Desk, our simulated monitoring centre, receives the request with the address, the household and the snapshot, and acknowledges it.

**5:49 · handling**  
The owner's phone now shows that the Desk is handling it.

**5:54 · closed**  
The Desk closes the request with an outcome, and that outcome becomes a label for future training.

**6:02 · diag**  
On the home network, the diagnostics page shows each model's score against its threshold, and how every input contributed.

**6:14 · live**  
And the owner can watch the live picture from anywhere, relayed through the Portal.

**6:21 · pi**  
On the Raspberry Pi in our demo room, the same flow runs on four-second clips. Its weapon and fire detectors are retrained on our own labelled recordings, and its thresholds were set from the room's own log.

**6:35 · results1**  
Our results. Training on our own footage raised validation macro-F1 from 0.28 to 0.77, and cut false alarms from about fourteen to 0.79 per normal minute. The evidence rules then removed the rest, and re-deciding our first live session took Level 3 alarms from nine to none.

**6:59 · results2**  
Retrained on our own recordings, the detectors' held-out AUROC rose from 0.75 to 0.87 for weapons, and from 0.59 to 0.96 for fire, with no new false alarms. Eleven of fourteen weapon episodes and every flame reach Level 3 on replay. In the demo room, calibration took scene-model false alarms in a five-hour log from forty-nine to zero. And the Pi analyses a four-second clip in 3.9 seconds.

**7:34 · limits**  
We also report the limits: too little normal footage to claim a false-alarm rate, a Pi that is still slower than its two-second target, and detectors that still need their own room calibration. Recording in the deployment room is our next step.

**7:51 · close**  
SENTRIX decides at home, and it asks the owner first. Thank you for watching.
