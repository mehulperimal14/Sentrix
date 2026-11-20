"""The storyboard: every scene's narration (caption, and the spoken form where the caption would be read badly).

Order follows the department's video guideline: title card (10 s+), then components, connections (README and
wiring), inputs, start/stop/troubleshooting, execution steps, final results. Every number is from a committed
report (CLAUDE.md section 4, the end-semester report).
"""

STUDENTS = [
    ("Kartik Garg", "102303478"),
    ("Akshay Ranveer", "102303453"),
    ("Prashant Gagneja", "102353011"),
    ("Harshit Mishra", "102319039"),
    ("Mehul Perimal", "102315144"),
]
TITLE = "SENTRIX: Intelligent Multimodal Edge Threat Intelligence and Physical Security System"
DIGITS = "zero one two three four five six seven eight nine".split()


def title_speech_team():
    return ("Sentrix: intelligent multimodal edge threat intelligence and physical security system. "
            "A capstone project by Kartik Garg, Akshay Ranveer, Prashant Gagneja, Harshit Mishra and Mehul Perimal, "
            "Thapar Institute of Engineering and Technology, guided by Doctor Ashutosh Mishra.")


def title_speech(name, roll):
    g = [roll[:3], roll[3:6], roll[6:]]
    spoken_roll = ", ".join(" ".join(("oh" if c == "0" else DIGITS[int(c)]) for c in part) for part in g)
    return (f"Sentrix: intelligent multimodal edge threat intelligence and physical security system. "
            f"Presented by {name}, roll number {spoken_roll}. Thapar Institute of Engineering and Technology.")


# The five role lines Kartik Garg recorded (third person); the narrator reads the same words in the other videos.
ROLES = [
    ("Kartik Garg", "102303478", "Vision and backend lead",
     "Detector scores into alarms we can trust; the architecture of the whole system",
     "Kartik led vision and the backend: turning detector scores into alarms we can trust, and the architecture of the whole system."),
    ("Akshay Ranveer", "102303453", "Identity lead",
     "Residents per tracked person, body re-identification, match bars from measured error rates",
     "Akshay led identity: recognising residents per tracked person, body re-identification, and setting the match bars from error rates we measured."),
    ("Prashant Gagneja", "102353011", "Audio lead",
     "Sound capture, AudioSet tagging, every sound window aligned with the video",
     "Prashant led audio: capturing sound, tagging it with an AudioSet model, and aligning every sound window with the video."),
    ("Harshit Mishra", "102319039", "Behaviour and fusion lead",
     "The data-collection protocol and the transparent rules behind one alarm level",
     "Harshit led behaviour and fusion: the data-collection protocol, and the transparent rules that combine every input into one alarm level."),
    ("Mehul Perimal", "102315144", "Records, interfaces, evaluation",
     "Tamper-evident evidence, the alarm screens, evaluation by the level a user sees",
     "Mehul led records, interfaces and evaluation: tamper-evident evidence storage, the alarm screens, and judging the product by the alarm level a user actually sees."),
]

# Each scene: (id, builder, chapter, lines, options). lines: [(caption, spoken or None)].
SCENES = [
    ("problem", "problem", 0, [
        ("Home alarms raise far more false alarms than real ones: police studies put them at 94 to 98 percent.",
         "Home alarms raise far more false alarms than real ones. Police studies put them at ninety-four to ninety-eight percent."),
        ("And cloud cameras send private video out of the home.", None),
    ], {}),
    ("hero", "hero", 0, [
        ("SENTRIX is our answer.", "Sentrix is our answer."),
        ("One small unit watches a room, and decides on the device itself whether there is a handgun or a visible flame.", None),
        ("It asks the owner before anyone else is alerted, and its video, faces and evidence stay on the device.", None),
    ], {}),
    ("team", "team", 0, [(r[4], None) for r in ROLES], {"captions": False}),
    ("agenda", "agenda", 0, [
        ("This video follows the six parts of the guideline: the components, the connections, the inputs, "
         "how to start, stop and troubleshoot it, a complete run, and the final results.", None),
    ], {}),
    # 1 components
    ("programs", "programs", 1, [
        ("SENTRIX has two programs.", "Sentrix has two programs."),
        ("The device runs on a Raspberry Pi 5: it owns the camera, runs every model, and serves its own screens.", None),
        ("The Portal runs in the cloud: it holds accounts, contacts and a responder desk, and reaches the device only "
         "through the device's own outbound link.", None),
    ], {}),
    ("modules", "modules", 1, [
        ("Inside the device, perception models produce votes: a YOLO person, weapon and fire detector, a DINOv2 scene "
         "classifier trained on our own footage, face recognition, body re-identification and a sound tagger.",
         "Inside the device, perception models produce votes: a yolo person, weapon and fire detector, a dino v two scene "
         "classifier trained on our own footage, face recognition, body re-identification, and a sound tagger."),
        ("Evidence rules confirm the votes, the threat confidence index and the level policy commit one of five levels, "
         "and the SOS engine asks the owner.",
         "Evidence rules confirm the votes. The threat confidence index and the level policy commit one of five levels, "
         "and the S O S engine asks the owner."),
        ("Every message passes one send guard, and test mode is on by default.", None),
    ], {}),
    # 2 connections
    ("physical", "physical", 2, [
        ("Our unit is a Raspberry Pi 5 with 8 GB of memory and an active cooler, powered by the official 27-watt USB-C supply.",
         "Our unit is a Raspberry Pi 5 with eight gigabytes of memory and an active cooler, powered by the official "
         "twenty-seven watt U S B C supply."),
        ("A Camera Module 3 sits on a tilting mount, a USB microphone listens, and three status LEDs show its state, "
         "all in our 3D-printed case.",
         "A Camera Module 3 sits on a tilting mount, a U S B microphone listens, and three status L E Ds show its "
         "state, all in our three D printed case."),
    ], {}),
    ("wiring", "wiring", 2, [
        ("The camera connects to the Pi's camera port with a ribbon cable, and the microphone goes into any USB port, "
         "even while SENTRIX is running.",
         "The camera connects to the Pi's camera port with a ribbon cable, and the microphone goes into any U S B port, "
         "even while Sentrix is running."),
        ("The green, amber and red LEDs are on GPIO 5, 12 and 13, through 330-ohm resistors.",
         "The green, amber and red L E Ds are on G P I O five, twelve and thirteen, through three hundred and thirty ohm resistors."),
        ("The official 27-watt USB-C supply powers the Pi.", "The official twenty-seven watt U S B C supply powers the Pi."),
        ("The README and the Pi guide in our repository list every pin.",
         "The read me and the Pi guide in our repository list every pin."),
    ], {}),
    # 3 inputs
    ("inputs", "inputs", 3, [
        ("SENTRIX needs a camera picture; sound from the USB microphone is optional.",
         "Sentrix needs a camera picture. Sound from the U S B microphone is optional."),
        ("To set it up, the owner activates an account with a one-time code, and enters the home's address, the "
         "household, emergency contacts, who confirm by consent, and a PIN and a duress PIN.", None),
        ("Residents' faces are enrolled from the device's own camera, so the people who live there are not treated as strangers.", None),
    ], {}),
    # 4 start / stop / troubleshoot
    ("start", "start", 4, [
        ("On the Pi, SENTRIX is a service that starts by itself at boot, and the status command shows it running.",
         "On the Pi, Sentrix is a service that starts by itself at boot, and the status command shows it running."),
        ("The Portal is hosted on Render and always on, so there is nothing to start: the owner simply opens it in a browser.", None),
    ], {}),
    ("stop", "stop", 4, [
        ("The same service commands stop or restart it.", None),
        ("The event log records every level change and every incident, and the doctor tool checks the models, the "
         "settings, the send guard and the evidence chain, without opening the camera.", None),
    ], {}),
    ("trouble", "trouble", 4, [
        ("Most problems have a short fix.", None),
        ("No picture: reseat the camera ribbon.", None),
        ("No sound: plug the microphone into any USB port; it is picked up automatically.",
         "No sound: plug the microphone into any U S B port. It is picked up automatically."),
        ("Device shown offline: check the Wi-Fi; the link reconnects on its own.",
         "Device shown offline: check the wifi. The link reconnects on its own."),
        ("And if a room gives false alarms, the calibration tool sets that room's thresholds from the device's own log.", None),
    ], {}),
    # 5 execution
    ("run", "wallclip", 5, [
        ("Now a complete run, in test mode, so nothing can be sent anywhere.", None),
        ("A held-out clip with a replica handgun, staged by our team, plays as the camera.", None),
    ], {"clip": "wall", "ss": 3.0, "min": 8.0, "label": "Device screen · wall view", "step": -1}),
    ("confirm", "wallclip", 5, [
        ("The scene model and the weapon detector both confirm the handgun, with a person in view.", None),
        ("Level 3 is committed, and the device screen switches to the alert.", "Level three is committed, and the device screen switches to the alert."),
    ], {"clip": "wall", "ss": 12.5, "min": 15.0, "label": "Device screen · wall view", "step": 0}),
    ("owner", "phoneclip", 5, [
        ("On the owner's phone a red bar appears, and the alert puts the three answers first.", None),
        ("The owner chooses Police and asks for help.", None),
    ], {"clip": "phone", "ss": 20.5, "min": 17.5, "label": "Owner's phone · SENTRIX Portal", "step": 2}),
    ("desk", "browserclip", 5, [
        ("The SENTRIX Desk, our simulated monitoring centre, receives the request with the address, the household and "
         "the snapshot, and acknowledges it.",
         "The Sentrix Desk, our simulated monitoring centre, receives the request with the address, the household and "
         "the snapshot, and acknowledges it."),
    ], {"clip": "desk", "ss": 0.0, "min": 10.5, "label": "SENTRIX Desk · simulated responder console", "step": 3}),
    ("handling", "phoneclip", 5, [
        ("The owner's phone now shows that the Desk is handling it.", None),
    ], {"clip": "phone", "ss": 54.0, "min": 5.5, "label": "Owner's phone · SENTRIX Portal", "step": 3}),
    ("closed", "browserclip", 5, [
        ("The Desk closes the request with an outcome, and that outcome becomes a label for future training.", None),
    ], {"clip": "desk", "ss": 16.0, "min": 8.5, "label": "SENTRIX Desk · simulated responder console", "step": 4}),
    ("diag", "browserclip", 5, [
        ("On the home network, the diagnostics page shows each model's score against its threshold, and how every input contributed.", None),
    ], {"clip": "diagnostics", "ss": 3.0, "min": 12.0, "label": "Device admin · Diagnostics (home network)", "step": None}),
    ("live", "browserclip", 5, [
        ("And the owner can watch the live picture from anywhere, relayed through the Portal.", None),
    ], {"clip": "live", "ss": 1.0, "min": 7.5, "label": "Owner · Live view through the Portal", "step": None}),
    ("pi", "pi", 5, [
        ("On the Raspberry Pi in our demo room, the same flow runs on four-second clips.", None),
        ("Its weapon and fire detectors are retrained on our own labelled recordings, and its thresholds were set "
         "from the room's own log.", None),
    ], {}),
    # 6 results
    ("results1", "results1", 6, [
        ("Our results.", None),
        ("Training on our own footage raised validation macro-F1 from 0.28 to 0.77, and cut false alarms from about "
         "fourteen to 0.79 per normal minute.",
         "Training on our own footage raised validation macro F one from zero point two eight to zero point seven seven, "
         "and cut false alarms from about fourteen to zero point seven nine per normal minute."),
        ("The evidence rules then removed the rest, and re-deciding our first live session took Level 3 alarms from nine to none.",
         "The evidence rules then removed the rest, and re-deciding our first live session took level three alarms from nine to none."),
    ], {}),
    ("results2", "results2", 6, [
        ("Retrained on our own recordings, the detectors' held-out AUROC rose from 0.75 to 0.87 for weapons, and from "
         "0.59 to 0.96 for fire, with no new false alarms.",
         "Retrained on our own recordings, the detectors' held-out A U R O C rose from zero point seven five to zero "
         "point eight seven for weapons, and from zero point five nine to zero point nine six for fire, with no new false alarms."),
        ("Eleven of fourteen weapon episodes and every flame reach Level 3 on replay.",
         "Eleven of fourteen weapon episodes, and every flame, reach level three on replay."),
        ("In the demo room, calibration took scene-model false alarms in a five-hour log from forty-nine to zero.", None),
        ("And the Pi analyses a four-second clip in 3.9 seconds.",
         "And the Pi analyses a four second clip in three point nine seconds."),
    ], {}),
    ("limits", "limits", 6, [
        ("We also report the limits: too little normal footage to claim a false-alarm rate, a Pi that is still slower "
         "than its two-second target, and detectors that still need their own room calibration.", None),
        ("Recording in the deployment room is our next step.", None),
    ], {}),
    ("close", "close", 0, [
        ("SENTRIX decides at home, and it asks the owner first.", "Sentrix decides at home, and it asks the owner first."),
        ("Thank you for watching.", None),
    ], {}),
]
