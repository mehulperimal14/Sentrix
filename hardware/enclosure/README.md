# SENTRIX enclosure, rev D

A 3D-printed case for the SENTRIX device: Raspberry Pi 5 with the Active
Cooler, Camera Module 3 in a separate tilting pod, a USB microphone, a
3-pin buzzer module and three status LEDs. It hangs on a wall (the main
configuration) or stands on a table.

![on the wall](renders/9_wall_mounted.png)

| File | What it is |
|---|---|
| `sentrix_enclosure.scad` | the parametric model (OpenSCAD 2021.01 or newer): every size is a named value at the top |
| `stl/` | one STL per part, already in print orientation: opens in every slicer |
| `3mf/` | the same parts as 3MF: Bambu Studio, PrusaSlicer, OrcaSlicer, Cura 5 |
| `renders/` | pictures: assembled, exploded, on the wall, the inside and the print plate |
| `build.py` | re-exports everything after a change; `--check` also runs the interference checks |

Every hole sits on the component's own hole, from Raspberry Pi Ltd's official
drawings: Raspberry Pi 5 (RP-008347), Active Cooler (RP-008188), Camera
Module 3 standard (RP-008153).

**Revisions.** Rev B: the first checked design. Rev C: the three status LED
windows of the earlier rev A design (the device code drives green, orange
and red LEDs on GPIO 5, 12, 13); rev A's I/O bay, PIR window and speaker
grille are left out because this build has no relay, PIR or speaker.
Rev D:

* the camera's ribbon connector is on its top edge (the drawing; rev B/C had
  it on the bottom), so the pod is 2.8 mm taller and the ribbon folds back
  over the camera and runs down behind it to the exit under the pivot;
* the pod turns on one M3 x 45 bolt through both arms and the pod with a
  nyloc nut, instead of two screws threaded into the pod: no thread is cut in
  the joint that moves;
* the wall keyholes moved onto the case's centre line, so it hangs straight;
* longer thread engagement for the Pi and bracket screws, thicker lid-screw
  bosses, and a `pilot_adj` setting for the pilot holes;
* the LED sleeves and the buzzer cup grip with crush ribs at a fixed
  diameter, so any standard part presses in and none sits loose.

## The parts

| Part | Print orientation | Size (mm) | PETG, solid | Supports |
|---|---|---|---|---|
| `sentrix_coupon` | flat | 74 x 30 x 18 | 12 g | none - **print this first** (about 30 min) |
| `sentrix_base` | floor down | 97 x 73 x 39 | 55 g | none |
| `sentrix_lid` | top face down (the SENTRIX lettering is on the bed) | 97 x 73 x 13 | 23 g | none |
| `sentrix_bracket` | base plate down | 28 x 38 x 16 | 5 g | none |
| `sentrix_pod` | lens face down | 36 x 30 x 15 | 7 g | none |
| `sentrix_pod_back` | flat | 35 x 30 x 4 | 3 g | none |

Weights are for solid parts at 1.27 g/cm3; at 20 % infill and 3 walls expect
roughly half. Every part fits a 120 x 120 mm bed, so any printer works
(Ender 3, Prusa MK3/MK4/Mini, Bambu A1/A1 mini/P1/X1, Anycubic, Creality K1
and so on).

## Print settings (any printer)

| Setting | Value |
|---|---|
| Material | **PETG** (best near the warm Pi) or PLA |
| Nozzle / layer height | 0.4 mm / 0.2 mm |
| Walls (perimeters) | 3; 4 for the bracket and the pod |
| Infill | 20 % gyroid (or grid) |
| Top / bottom layers | 4 / 4 |
| Supports | none |
| Brim | only if your PETG lifts at the corners |

**Fit coupon first.** It has a Pi standoff, a camera post, an M3 boss (one
pilot of each size), the buzzer cup, an LED sleeve, a USB-A window and the
three board hole sizes. Then adjust in `sentrix_enclosure.scad`:

| If | Change |
|---|---|
| a screw is too hard to drive or a boss splits | `pilot_adj` up by 0.1 |
| a screw strips | `pilot_adj` down by 0.1 |
| the LED is too hard to push in / loose | `led_grip` 4.9 / 4.7 (default 4.8) |
| the buzzer is too hard to push in / loose | `buzz_grip` 11.7 / 11.5 (default 11.6) |
| holes and slots print small in general | `tol` up (default 0.20) |

Then run `python build.py`, or export from OpenSCAD: set `PART`, press F6,
then File > Export.

## Where each component goes

| Component | Placement |
|---|---|
| Raspberry Pi 5 | screwed down with 4 x M2.5 on four standoffs at its own holes (58 x 49 mm grid), 5 mm above the floor; the cooler clips into its own two holes |
| Active Cooler | hex intake in the lid right over the blower, hex exhaust over the fins, slots in the end and back walls |
| USB ports + Ethernet | the camera end of the case; the wall there is only 1 mm thick, so the USB microphone plugs in fully and faces outwards |
| USB-C power, 2 x micro-HDMI | three openings in the long side: USB-C 13.5 x 8.5 mm, each micro-HDMI 11.6 x 7.6 mm, sized for the plug bodies |
| Power button | the end opposite the camera: a round flexure in the wall with a 3 mm nub 0.3 mm from the Pi's button. Press once to switch on; while it runs, press twice for a clean shutdown; hold it to force the Pi off |
| Status LED (the Pi's own) | visible through the slot beside the power button |
| microSD card | a 13 x 3.8 mm slot under the power button: push the card in through it; pull it out with tweezers, or stick a small tape tab on the card |
| Buzzer (cylindrical, no screw holes) | pressed into a 12.8 mm cup under the lid; three crush ribs narrow it to 11.6 mm so a standard 12 mm buzzer is gripped; the module's board stops on the rim; seven sound holes in the lid face the room |
| Status LEDs (3 x 5 mm) | pressed into sleeves under the lid from inside; three crush ribs narrow the 5.4 mm bore to 4.8 mm so any 5 mm LED is gripped; the LED's 5.6-6.0 mm rim stops on the sleeve and the dome sits flush |
| Camera Module 3 | bolted inside the pod with 4 x M2 into four posts on its own holes (21 x 12.5 mm grid); the lens sits 0.6 mm behind the window |
| Camera ribbon | from the connector on the camera's top edge it folds back over the camera, runs down behind it and leaves the pod through its floor right at the pivot, then into the case through the bracket and lid slots: tilting barely moves it |
| Tilt pivot | one M3 x 45 bolt through both bracket arms and the pod, below the camera and beside the ribbon, with a nylon washer each side and a nyloc nut |
| SENTRIX | engraved on the lid, and small on the pod's top |

## Screws and parts to buy

| Qty | Part | For |
|---|---|---|
| 4 | M2.5 x 8 mm, pan head | Raspberry Pi 5 to the standoffs |
| 4 | M2 x 6 mm, pan head | Camera Module 3 to the pod's posts |
| 2 | M2 x 6 mm, pan head | pod back cover |
| 4 | M3 x 10 mm, pan head | lid to base |
| 4 | M3 x 10 mm, countersunk | bracket to lid |
| 1 | M3 x 45 mm bolt + M3 nyloc nut + 2 nylon washers | pod pivot |
| 3 | 5 mm LEDs (green, orange, red) + 330 ohm resistors | status lights |
| 1 | Raspberry Pi 5 camera cable, 200 mm (22-pin to 15-pin) | camera |
| 2 | wall screws (shank 3.5-4 mm, head 6-8 mm) + plugs | wall use |
| 4 | rubber feet, 10 mm dia | table use only |

## Screw grip

Every screw except the pivot bolt cuts its own thread into a pilot hole about
85 % of its size (M2 1.6, M2.5 2.1, M3 2.5 mm). Rule of thumb for printed
PETG: thread engaged at least 2 x the screw's diameter and a wall of 1.2 mm
or more round the hole.

| Joint | Screw | Thread engaged | Wall round the hole |
|---|---|---|---|
| Pi to base standoffs | M2.5 x 8 | 6.4 mm (2.6 x d) | 1.95 mm |
| Lid to base corners | M3 x 10 pan | 8.8 mm (2.9 x d) | 1.85-2.45 mm |
| Bracket to lid | M3 x 10 countersunk | 7.0 mm (2.3 x d) | 2.0 mm |
| Camera to pod posts | M2 x 6, from behind | 4.9 mm (2.4 x d) | 1.2 mm |
| Pod back cover | M2 x 6 | 4.6 mm (2.3 x d) | 1.4 mm |
| Pod pivot | M3 x 45 bolt + nyloc nut | none: smooth 3.6 mm holes | 5.9 mm of bearing each side |

The pivot is the one joint that moves, so it has no thread in the plastic:
the bolt turns in smooth holes, and the nyloc nut sets how stiff the tilt is
and never works loose. Brass heat-set inserts are not needed; the bosses are
sized for thread-forming screws.

## Assembly

1. **Pi into the base.** Fit the Active Cooler on the Pi and plug its fan
   cable into FAN. Insert the microSD card. Lower the Pi onto the standoffs,
   power button towards the flexure end, and screw it down (4 x M2.5 x 8).
2. **Camera ribbon.** Plug the 22-pin end into the Pi's CAM/DISP 0 connector.
   Thread the other end up through the slot in the lid, then through the
   bracket's slot.
3. **Bracket onto the lid** (4 x M3 x 10 countersunk), with the ribbon in its
   slot.
4. **Camera into the pod.** Push the ribbon up through the slot in the pod's
   floor and plug it into the connector on the camera's top edge (contacts
   towards the board). Fold the ribbon back over the top of the camera and
   lay it down the back. Set the camera on the four posts, lens through the
   window, and screw it on from behind (4 x M2 x 6; they sit beside the
   ribbon). Close the back cover (2 x M2 x 6).
5. **Pod into the bracket.** Push the M3 x 45 bolt through one arm, the pod
   and the other arm, with a nylon washer under the head and under the nut.
   Tighten the nyloc nut until the pod clicks firmly every 15 degrees and
   stays where you set it.
6. **Buzzer and LEDs.** Wire the buzzer module to the GPIO: VCC to pin 2
   (5 V), GND to pin 39, I/O to pin 37 (GPIO26). Wire each LED's anode through
   a 330 ohm resistor: green to pin 29 (GPIO5), orange to pin 32 (GPIO12),
   red to pin 33 (GPIO13); all three cathodes to pin 34 (GND). Press the
   buzzer up into the cup under the lid until its board meets the rim, and
   the LEDs into their sleeves from inside until their rims stop.

   The code drives the LEDs on its own: green solid when the unit works and
   the Portal link is up; orange fast blink on a fault, solid with no network;
   red slow blink at Level 3 or an open SOS, fast at Level 4, solid at Level 5.
7. **Close the case** (4 x M3 x 10 through the lid corners).
8. Hang it (below), then plug the USB microphone into a USB port at the
   bottom end and the USB-C power cable into the side.

## Wall mounting

Two keyhole slots under the base, 42 mm apart on the case's centre line.
Put two screws in the wall one above the other, 42 mm apart, heads 2-3 mm
proud. Hang the case with the camera end down: the heads go into the round
holes, then slide the case down. The keyholes are closed on the inside, so no
dust or screw tip reaches the Pi. Leave the rubber feet off for the wall.

On the wall the lid faces the room: the cooler draws air through the lid
grille, warm air leaves through the slots in the top end, the USB microphone
points down from the bottom end and the USB-C cable leaves from the side.

## Camera angle

The pod tilts 0-90 degrees and clicks every 15 degrees. The scale is on the
pod's side, and the notch on the bracket arm points at the current angle.

* **On a wall:** 0 degrees looks straight down the wall and 90 straight out.
  **45 degrees** (the long tick) suits a unit above head height.
* **On a table:** 0 degrees looks level along the table; 15-30 looks slightly
  up into the room.

## What was checked, and what was not

* An exact geometry check (`python build.py --check`) intersects each part
  with the space each component needs: the board, every port, the cooler,
  the GPIO header with DuPont plugs on it, the microSD card, the cooler's push
  pins, the button, the camera with its lens, top-edge connector and screw
  heads, the buzzer, and the LEDs with their legs. It also checks the
  ribbon's whole path inside the pod against the pod, the cover and the pivot
  bolt; the pivot bolt against the camera and the pod; the lid against the
  base; the back cover against the pod; and the pod against the bracket and
  lid at every 15 degrees of tilt. The click-detent bumps, which press
  0.25 mm into the pod on purpose, are left out of the tilt check.
* **Not yet printed.** Print the coupon first: it shows whether the screws,
  the buzzer and an LED fit your printer before the big parts. The USB
  microphone stays outside the case, so its body size does not matter.
