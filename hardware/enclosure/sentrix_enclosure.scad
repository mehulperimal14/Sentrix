// SENTRIX enclosure, rev E.  Rev C added rev A's status LED windows; rev D the
// wall keyholes on the centre line, longer screw engagement, gripping LED and
// buzzer seats and the pivot bolt; rev E mounts the camera connector-down (its
// upright orientation: every earlier revision gave an upside-down picture), so
// the ribbon drops straight out of the pod, and takes the parts audit's
// corrections (lens depth, connector depth, wall clearances, vents, cooler
// outline, the ribbon's path through the case)
// Raspberry Pi 5 + Active Cooler + Camera Module 3 (standard) + USB microphone
// + 3-pin buzzer module + three 5 mm status LEDs.  Open in OpenSCAD (2021.01 or newer), set PART, press
// F6, then File > Export > STL (or 3MF).  hardware/enclosure/README.md has the
// print settings, the screw list and the assembly order.
//
// Dimensions come from Raspberry Pi Ltd's drawings: Raspberry Pi 5 (RP-008347),
// Active Cooler product brief (RP-008188), Camera Module 3 standard
// (RP-008153).  Every screw hole sits on the component's own hole.
//
// Frame (mm): x, y are the Pi 5 board's own coordinates: origin at the board
// corner by the USB-C connector; x along the 85 mm edge towards the USB and
// Ethernet ports; y along the 56 mm edge towards the GPIO header.  z = 0 is
// the underside of the base.

include <thapar_ti_mark.scad>

PART = "assembly"; // [assembly, wall, exploded, base, lid, bracket, pod, pod_back, coupon, print_plate]
TILT = 45;         // camera tilt in the assembly view: 0 = level, 90 = straight up

/* [Fit] */
tol      = 0.20;   // added to every clearance hole and slot (raise for printers that print holes small)
fit      = 0.25;   // sliding fit per side (lid lip, pod cover, pod in the fork)
// Every screw cuts its own thread in a pilot hole (thread-forming), engaged at
// least 2 x its diameter (every joint has 2.3 x or more), in a boss with at
// least 1.2 mm of wall around it.
// The fit coupon has one pilot of each size: if a screw is too hard to drive or
// a boss splits, raise pilot_adj by 0.1; if a screw strips, lower it.
pilot_adj = 0.0;
pilot_m3  = 2.50 + pilot_adj;  // lid, bracket and pivot screws
pilot_m25 = 2.10 + pilot_adj;  // Pi screws
pilot_m2  = 1.60 + pilot_adj;  // camera and pod-cover screws
clear_m3  = 3.40;  // M3 clearance
clear_m2  = 2.40;  // M2 clearance

/* [Shell] */
wall    = 2.4;
floor_t = 2.4;
lid_t   = 2.6;
r_out   = 4.0;     // outer corner radius
standoff_h = 5.0;  // floor top -> board underside (microSD, cooler push pins)
pcb_t   = 1.6;
above   = 30.0;    // board top -> lid underside (cooler 13.7, DuPont plugs ~17, buzzer cup)

// interior box in board coordinates
ix0 = -3.5;        // left: power button (0.45 proud), microSD card, flexure nub
ix1 = 88.6;        // right: Ethernet face 87.98, USB stacks' tabs 88.30 (0.3 to spare as the Pi drops in)
iy0 = -2.0;        // front: USB-C face -1.31, micro-HDMI flares -1.68 (0.3 to spare)
iy1 = 66.0;        // back: room behind the GPIO header for the buzzer wires

board_bot = floor_t + standoff_h;      // 7.4
board_top = board_bot + pcb_t;         // 9.0
wall_top  = board_top + above;         // 39.0
lid_top   = wall_top + lid_t;          // 41.6

$fn = 48;
e = 0.01;

// ------------------------------------------------------------------ Pi 5 data
pi_holes   = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];  // dia 2.7, M2.5
cool_pins  = [[3.5, 9.5], [61.5, 46.5]];                             // Active Cooler push pins
// Active Cooler (RP-008188): heatsink and push-pin heads x 0.33-64.28,
// y 6.50-49.66, but open at the front right (x > 44.3, y < 18): the diagonal
// edge and the fan box start at y 18.0, so the camera connectors and the
// ribbon riser are clear.  Two boxes cover it with margin.
// The front strip (x 27.4-44.3, y < 17.9) is only the base plate, about 4 mm
// above the board top; the fins and the fan box are full height.
cool_boxes = [[[0.3, 6.5], [27.4, 49.7]], [[27.4, 17.9], [64.3, 49.7]]];
cool_plate = [[27.4, 6.5], [44.3, 17.9]];  cool_plate_h = 4.5;
cool_h     = 15.5;    // upper bound above the board top: heatsink body 10.5 above its base + SoC and pad
fan_c      = [41.8, 33.7];                    // blower intake centre (dia 21.2, housing 30 x 30)
fins_box   = [[5.0, 9.0], [26.5, 49.0]];      // pin fins x 7.5-26.5, y 9.0-49.0 (lid grille over them)
// ports: [lo, hi] along the edge, [z lo, z hi] relative to the board top
eth   = [[2.25, 18.15], [-2.05, 13.70]];      // right edge, centre y 10.2
usb_a = [[21.70, 36.50], [-3.50, 16.80]];     // right edge, centre y 29.1
usb_b = [[39.60, 54.40], [-3.50, 16.80]];     // right edge, centre y 47.0
usbc  = 11.2;  hdmi0 = 25.8;  hdmi1 = 39.2;   // front edge connector centres (x)
sd_y  = [22.0, 34.0];                         // microSD on the underside, left edge
pwr_y = 18.4;  led_y = 13.3;                  // power button / status LED on the left edge
pwr_z = 1.6;                                  // button plunger centre above the board top
gpio  = [[7.1, 49.4], [57.9, 55.6]];          // 2 x 20 header
cam_fpc = [[47.24, 50.20], [53.45, 56.38]];   // CAM/DISP connector bodies (x), both y 0.68-16.17, 4.1 tall
cam_fpc_y = [0.68, 16.17];

// --------------------------------------------------------- Camera Module 3 data
// Pod frame: X = optical axis (forward), Z = up, Y = sideways; origin = lens
// centre on the PCB's front face.  PCB 25 x 23.862 x 1.12 (RP-008153).
// Mounted CONNECTOR-DOWN: the picture is upright that way with no software
// transform (the IMX708's device-tree rotation is 180 for a board held with
// the connector at the bottom, and libcamera's default undoes it; Raspberry
// Pi's own photo shows the ribbon leaving the bottom).  The drawing draws the
// board the other way up.  The ribbon connector (19.61 wide, 5.71 tall, 2.75
// deep) is on the back at the edge nearer the lens: the lens centre is 9.462
// above the bottom (connector) edge and 14.4 below the top edge; the M2 holes
// (dia 2.2) are 2.0 from the sides and 2.0 and 14.5 below the top edge.
cam_w = 25.0;  cam_h = 23.862;  cam_t = 1.12;
cam_conn_w = 19.61;  cam_conn_h = 5.71;
cam_z1 = 14.4;                                  // top edge above the lens centre
cam_z0 = cam_z1 - cam_h;                        // -9.462: the connector edge, at the bottom
cam_holes = [[-10.5, cam_z1 - 2.0], [10.5, cam_z1 - 2.0],
             [-10.5, cam_z1 - 14.5], [10.5, cam_z1 - 14.5]];   // (Y, Z): 12.4 and -0.1
// lens tip in front of the PCB front face: the drawing's 6.98 ends on a 0.40
// layer and the brief gives 11.5 +-0.2 overall, so 6.98-7.83; sized for 7.63
// (a 7.83 tip still sits 0.4 behind the pod's face)
cam_tip  = 7.63;
cam_back = 2.75;        // connector behind the PCB back face (drawing)
fpc_w    = 16.0;        // 15-pin ribbon at the camera end (the Pi end is 11.5)

// pod geometry (pod frame)
pod_wall = 2.0;
pod_front_out = cam_tip + 0.6;                  // lens tip 0.6 behind the front face
pod_front_in  = pod_front_out - pod_wall;       // standoffs reach from here to the PCB
pod_back_out  = -9.5;
pod_back_in   = pod_back_out + pod_wall;
pod_y  = cam_w / 2 + 0.6 + pod_wall;            // half width 15.1
pod_z1 = cam_z1 + 0.6 + pod_wall;               // top 17.0
pod_z0 = -20.9;                                 // bottom: the ribbon's drop and the pivot bolt below the camera
pod_r  = 3.0;
piv_p  = [-4.5, -18.0];                         // pivot (X, Z) in the pod frame: low, just in front of the back cover
piv_boss_d = 5.6;                               // fits inside the pod's back-bottom corner
piv_boss_l = 3.9;                               // bearing boss inside each side wall (5.9 mm of bearing with the wall)
// the pod turns on one M3 x 45 bolt through both arms and the pod (below the
// camera, behind the ribbon), with a nylon washer on each side and a nyloc nut:
// smooth holes only, so tilting never wears a thread or works anything loose
// pod cover screws (Y, Z): below the camera board, in full-depth columns from
// the front wall (they print upright and leave the camera a straight way in)
cover_screws = [[-11.6, -12.3], [11.6, -12.3]];
// the ribbon leaves the pod through its floor in front of the pivot bolt; the
// slot's back wall stays 1 mm in front of the bolt so a taut ribbon cannot
// reach it (0.9 mm or more at any tilt), and the outer back edge it is pulled
// over is rounded; 0.86 mm or more of floor stays round the bolt
pod_exit = [-2.0, 1.1];                         // X range of the floor slot
pod_exit_round = 1.5;

// fork bracket on the lid (board coordinates)
piv_x = 72.0;  piv_y = 15.0;
base_t = 3.0;
piv_h  = 7.0;                                   // pivot above the bracket base (the pod's rounded back corner swings 5.0 below it)
piv_z  = lid_top + base_t + piv_h;
arm_t  = 3.5;
arm_gap = pod_y + fit;                          // inner faces of the arms
br_x = [piv_x - 14, piv_x + 14];                // base plate along x (arms span piv_x +-6)
br_y = [piv_y - arm_gap - arm_t, piv_y + arm_gap + arm_t];
// screws beside the arms (x) and far enough in (y) that their bosses under the
// lid stay clear of the front wall
br_screws = [[br_x[0] + 3, br_y[0] + 8], [br_x[1] - 4, br_y[0] + 8],
             [br_x[0] + 3, br_y[1] - 6], [br_x[1] - 4, br_y[1] - 6]];
// lid + bracket slot right under the pod's floor slot (the pod's exit sits
// 2.5-5.6 in front of the pivot axis); tilting 0-90 degrees draws 4.4 mm of ribbon
cable_slot = [[piv_x + 1.0, piv_y - 9.0], [piv_x + 6.0, piv_y + 9.0]];

// buzzer: a cylinder with no screw holes, held in a cup under the lid
// a standard 12 mm buzzer (body 11.8-12.2, 8.5-9.5 tall): three crush ribs
// narrow the 12.8 cup to 11.6, so every body is gripped (0.1-0.3 mm per rib)
// and none sits loose; the module's board stops on the cup's rim.  If the
// coupon's cup is too tight, raise buzz_grip to 11.7; if loose, 11.5.
buzz_d = 12.0;  buzz_h = 9.5;  buzz_pcb = 1.6;  buzz_grip = 11.6;
buzz_c = [76.0, 43.0];
buzz_cup_wall = 1.6;

// status LEDs (5 mm, pushed into sleeves under the lid from inside; the code
// drives GPIO 5 / 12 / 13 as green = working, amber = attention, red = alarm)
leds = [[63.0, 58.5], [69.5, 58.5], [76.0, 58.5]];
// any standard 5 mm LED (body 4.9-5.2, rim 5.6-6.0, 8.6 tall over the rim):
// three crush ribs narrow the 5.4 bore to 4.8, so every body is gripped
// (0.05-0.2 mm per rib); the bore is narrower than the rim, so the rim stops
// on the sleeve's lower end and the dome sits flush with the lid.  If the
// coupon's LED is too hard to push in, raise led_grip to 4.9; if loose, 4.7.
led_d = 5.0;  led_bore = led_d + 0.2 + tol;  led_grip = 4.8;
led_sleeve = 7.4;  led_sleeve_h = 5.0;
label_x = 30.5;                                 // SENTRIX lettering centre, left of the LEDs

/* [Lettering] */
// Raised lettering on the base's long sides, printed white and coloured by
// hand: the institute's mark and name on the front (right of the ports), the
// project team on the back.  Raised, not engraved: 0.5 mm strokes print
// cleanly on a vertical wall and a paint marker colours just the letters.
lettering = true;
letter_h  = 0.6;                                // proud of the wall
letter_font = "Liberation Sans:style=Bold";     // bundled with OpenSCAD
mentor = "Mentor : Dr. Ashutosh Mishra";
team = [["KARTIK GARG", "102303478"], ["MEHUL PERIMAL", "102315144"], ["HARSHIT MISHRA", "102319039"],
        ["PRASHANT GAGNEJA", "102353011"], ["AKSHAY RANVEER", "102303453"]];

// lid screws (board coordinates), fused into the corners of the base:
// [x, y, boss dia, dia of its top 12 mm where the M3 thread is cut]
// (each overlaps both walls by 0.3 mm or more over its full height)
lid_bosses = [[-1.6, -0.7, 5.2, 6.2], [86.6, -0.6, 5.2, 6.2], [-0.8, 63.3, 6.0, 7.4], [85.9, 63.3, 6.0, 7.4]];
// wall keyholes: final screw position (the slot's rounded closed end); the
// slot runs +x to the entry hole.  On the case's centre line, so it hangs
// straight.  A 1.2 mm lip slides behind the screw head; the head channel is
// 3.2 mm tall, for pan or round heads up to 2.8 mm tall and 8.4 mm across.
// Their pads stay under the Pi (pad top 5.2; board underside 7.4, the USB
// stacks' pegs 5.46)
keyholes = [[22.0, 32.0], [64.0, 32.0]];
key_len = 8.0;
key_lip = 1.2;  key_head = 3.2;
feet = [[12.0, 2.0], [75.0, 2.0], [12.0, 62.0], [75.0, 62.0]];   // clear of the Pi screws' pilots
pi_pilot_z = 0.6;                               // Pi screw pilots run into the floor: an M2.5 x 8 engages 6.4 mm

// ================================================================== helpers
module rrect(x0, y0, x1, y1, r, h) {
    hull() for (x = [x0 + r, x1 - r], y = [y0 + r, y1 - r])
        translate([x, y, 0]) cylinder(r = r, h = h);
}

module box(a, b) { translate(a) cube([b[0] - a[0], b[1] - a[1], b[2] - a[2]]); }

// a horizontal hole along +y that prints without support (45 degree roof)
module teardrop_y(d, len) {
    // 2D -y becomes +z after rotate([-90, 0, 0]): the point is the roof
    rotate([-90, 0, 0]) linear_extrude(len)
        hull() { circle(d = d); translate([0, -d / 2 * sqrt(2)]) square(0.01, center = true); }
}

module hex_hole(af, h) { cylinder(d = af / cos(30), h = h, $fn = 6); }

// hexagon holes filling a 2D region (children = the region)
module hex_field(x0, y0, x1, y1, af = 4.0, rib = 1.4, h = 10) {
    pitch = af + rib;
    intersection() {
        union() for (i = [0 : floor((x1 - x0) / (pitch * cos(30))) + 1],
                     j = [0 : floor((y1 - y0) / pitch) + 1])
            translate([x0 + i * pitch * cos(30), y0 + j * pitch + (i % 2) * pitch / 2, 0])
                hex_hole(af, h);          // flats face the neighbours: ribs are 'rib' wide
        linear_extrude(h) children();
    }
}

// ================================================================== components (ghosts)
module pi5() {
    color("seagreen") translate([0, 0, board_bot]) rrect(0, 0, 85, 56, 3, pcb_t);
    // cooler
    color("silver") translate([0, 0, board_top]) {
        for (b = cool_boxes) box([b[0][0], b[0][1], 0], [b[1][0], b[1][1], cool_h]);
        box([cool_plate[0][0], cool_plate[0][1], 0], [cool_plate[1][0], cool_plate[1][1], cool_plate_h]);
    }
    color("dimgray") translate([fan_c[0], fan_c[1], board_top + cool_h - 0.1]) cylinder(d = 21.2, h = 0.5);
    // ports (the USB stacks' tabs reach x 88.3, the micro-HDMI flares y -1.7)
    color("lightgray") {
        for (p = [eth, usb_a, usb_b])
            box([85 - 17, p[0][0], board_top + p[1][0]], [88.3, p[0][1], board_top + p[1][1]]);
        translate([usbc - 4.47, -1.31, board_top]) cube([8.94, 7.5, 3.2]);
        for (c = [hdmi0, hdmi1]) translate([c - 3.6, -1.7, board_top]) cube([7.2, 8.56, 3.4]);
    }
    color("black") translate([gpio[0][0], gpio[0][1], board_top]) cube([50.8, 6.2, 8.5]);
    color("white") for (x = cam_fpc) translate([x[0], cam_fpc_y[0], board_top]) cube([x[1] - x[0], cam_fpc_y[1] - cam_fpc_y[0], 4.1]);
    // microSD card (sticks out 2.5 under the left edge)
    color("royalblue") translate([-2.5, sd_y[0] + 0.5, board_bot - 1.2]) cube([14, 11, 1.0]);
    // power button plunger
    color("red") translate([-0.45, pwr_y - 1.5, board_top]) cube([2, 3, 3.2]);
}

// the space the Pi needs (checked against the case: must not intersect)
module pi5_envelope() {
    translate([0, 0, board_bot + e]) rrect(0, 0, 85, 56, 3, pcb_t - 2 * e);
    for (b = cool_boxes) translate([0, 0, board_top]) box([b[0][0], b[0][1], 0], [b[1][0], b[1][1], cool_h]);
    translate([0, 0, board_top]) box([cool_plate[0][0], cool_plate[0][1], 0], [cool_plate[1][0], cool_plate[1][1], cool_plate_h]);
    for (p = [eth, usb_a, usb_b])
        box([85 - 17, p[0][0], board_top + p[1][0]], [88.3, p[0][1], board_top + p[1][1]]);
    translate([usbc - 4.47, -1.31, board_top]) cube([8.94, 7.5, 3.2]);
    for (c = [hdmi0, hdmi1]) translate([c - 3.6, -1.7, board_top]) cube([7.2, 8.56, 3.4]);
    for (x = cam_fpc) translate([x[0], cam_fpc_y[0], board_top]) cube([x[1] - x[0], cam_fpc_y[1] - cam_fpc_y[0], 4.1]);
    // GPIO header with DuPont plugs on it (17 mm above the board)
    box([gpio[0][0] - 0.6, gpio[0][1] - 1.2, board_top], [gpio[1][0] + 0.6, gpio[1][1] + 1.2, board_top + 17.0]);
    translate([-2.5, sd_y[0] + 0.5, board_bot - 1.2]) cube([14, 11, 1.0]);
    translate([-0.45, pwr_y - 1.5, board_top]) cube([2, 3, 3.2]);
    // cooler push-pin tips under the board
    for (p = cool_pins) translate([p[0], p[1], board_bot - 3.2]) cylinder(d = 3.5, h = 3.2);
}

module camera3() {   // in the pod frame, connector-down
    color("seagreen") translate([-cam_t, -cam_w / 2, cam_z0]) cube([cam_t, cam_w, cam_h]);
    color("dimgray") translate([0, -5.4, -5.4]) cube([3.875, 10.8, 10.8]);
    color("black") rotate([0, 90, 0]) cylinder(d = 5.75, h = cam_tip);
    color("white") translate([-cam_t - cam_back, -cam_conn_w / 2, cam_z0]) cube([cam_back, cam_conn_w, cam_conn_h]);
}

// the camera's way in: each part slid straight along the optical axis from
// 30 mm behind to 0.05 short of its seat (checked against the pod: the
// module goes onto its posts from the open back without tilting)
module camera_insertion_path() {
    module slide() hull() { translate([-30, 0, 0]) children(); translate([-0.05, 0, 0]) children(); }
    slide() translate([-cam_t, -cam_w / 2, cam_z0]) cube([cam_t, cam_w, cam_h]);
    slide() translate([-cam_t - cam_back, -cam_conn_w / 2, cam_z0]) cube([cam_back, cam_conn_w, cam_conn_h]);
    slide() translate([0, -5.4, -5.4]) cube([4.275, 10.8, 10.8]);
    slide() rotate([0, 90, 0]) cylinder(d = 6.14, h = cam_tip + 0.2);
}

// the pivot bolt's shank where it crosses the pod (pod frame)
module pivot_bolt() {
    translate([piv_p[0], 0, piv_p[1]]) rotate([90, 0, 0]) cylinder(d = 3.0, h = 2 * pod_y, center = true);
}

// the camera ribbon inside the pod (pod frame): straight down out of the
// connector at the camera's bottom edge, eased forward past the pivot bolt
// (1.1 mm clear of it), and out through the floor slot - no fold, no twist
module ribbon_envelope() {
    rb = fpc_w / 2 + 0.3;
    hull() {
        translate([-3.4, -rb, cam_z0 - 3.0]) cube([2.2, 2 * rb, 3.0]);
        translate([pod_exit[0] + 0.1, -rb, cam_z0 - 5.5]) cube([1.4, 2 * rb, 0.1]);
    }
    translate([pod_exit[0] + 0.1, -rb, pod_z0 - 0.5]) cube([1.4, 2 * rb, cam_z0 - 5.4 - pod_z0 + 0.5]);
}

module camera3_envelope() {
    translate([-cam_t + e, -cam_w / 2, cam_z0]) cube([cam_t - 2 * e, cam_w, cam_h]);
    translate([0, -5.4, -5.4]) cube([4.275, 10.8, 10.8]);          // housing, up to 0.4 deeper than drawn
    rotate([0, 90, 0]) cylinder(d = 6.14, h = cam_tip + 0.2);      // barrel (6.14 at its base), deepest tip
    translate([-cam_t - cam_back, -cam_conn_w / 2, cam_z0]) cube([cam_back, cam_conn_w, cam_conn_h]);
    // M2 screw heads on the back of the board
    for (h = cam_holes) translate([-cam_t - 1.6, h[0], h[1]]) rotate([0, 90, 0]) cylinder(d = 3.8, h = 1.6);
}

module buzzer() {   // module board + cylinder, as seated: the board on the cup's rim, the top 0.5 under the sound holes
    color("black") translate([buzz_c[0], buzz_c[1], wall_top - buzz_h - 0.5]) cylinder(d = buzz_d, h = buzz_h);
    color("navy") translate([buzz_c[0] - 9.25, buzz_c[1] - 7.5, wall_top - buzz_h - 0.5 - buzz_pcb])
        cube([18.5, 15, buzz_pcb]);
}

module leds5() {    // 5 mm LEDs (8.6 mm over the flange) seated in their sleeves: dome flush with the lid
    body = lid_top - led_d / 2 - (wall_top - led_sleeve_h);
    for (i = [0 : 2]) translate([leds[i][0], leds[i][1], wall_top - led_sleeve_h]) {
        color(["limegreen", "orange", "red"][i]) {
            translate([0, 0, -1.0]) cylinder(d = 5.8, h = 1.0);
            cylinder(d = led_d, h = body);
            translate([0, 0, body]) sphere(d = led_d);
        }
        color("silver") for (dx = [-1.27, 1.27]) translate([dx - 0.25, -0.25, -15]) cube([0.5, 0.5, 14]);
    }
}

// screws, for the pictures: Pi (M2.5 pan), lid (M3 pan in counterbores),
// bracket (M3 countersunk, flush), pivot (M3 + washer), camera (M2 from behind)
module screws(a = TILT) {
    color("silver") {
        for (h = pi_holes) translate([h[0], h[1], board_top]) cylinder(d = 4.5, h = 1.8);
        for (b = lid_bosses) translate([b[0], b[1], lid_top - 1.4]) cylinder(d = 5.6, h = 1.4);
        for (s = br_screws) translate([s[0], s[1], lid_top + base_t - 0.2]) cylinder(d = 6.0, h = 0.25);
        // pivot bolt (local +z = world +y): washer, head, shank; washer and nyloc nut on the far side
        translate([piv_x, 0, piv_z]) rotate([-90, 0, 0]) {
            translate([0, 0, br_y[0] - 3.5]) cylinder(d = 5.5, h = 3.0);
            translate([0, 0, br_y[0] - 0.5]) cylinder(d = 3.0, h = 45);
            for (z = [br_y[0] - 0.5, br_y[1]]) translate([0, 0, z]) cylinder(d = 7.0, h = 0.5);
            translate([0, 0, br_y[1] + 0.5]) cylinder(d = 6.35, h = 4.0, $fn = 6);
        }
        pod_placed(a) for (h = cam_holes) translate([-cam_t - 1.4, h[0], h[1]]) rotate([0, 90, 0]) cylinder(d = 3.6, h = 1.4);
    }
}

// two wall screws in their keyholes (heads in the channel), for the wall picture
module wall_screws() {
    color("silver") for (k = keyholes) translate([k[0], k[1], 0]) {     // pan head 7.6 x 2.6
        translate([0, 0, key_lip + 0.2]) cylinder(d = 7.6, h = 2.6);
        translate([0, 0, -30]) cylinder(d = 3.8, h = 30 + key_lip + 0.2);
    }
}

// the case hung on a wall: camera end down, lid facing the room (world z up,
// the wall is the plane y = 0 and the room is on -y)
module on_wall() { multmatrix([[0, 1, 0, 0], [0, 0, -1, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) children(); }

module wall_mounted() {
    color("tan") translate([-45, 0, -150]) cube([160, 6, 200]);
    on_wall() { assembly(); screws(); wall_screws(); }
}

// the camera ribbon inside the case (board coordinates): up from the CAM/DISP
// connector nearer the HDMI ports, across above the Ethernet jack, up through
// the lid slot.  The spare length goes in the pocket over the cooler's low
// front plate, between the micro-HDMI connectors and the fan - never over
// the lid's intake grille or the fins grille: a 200 mm cable's ~12 cm as a
// serpentine of three upright legs with bends no tighter than r 4 (a pencil),
// or a 500 mm cable's ~40 cm folded in half and rolled loosely around a
// pencil (about 14 mm across, 11.5 wide).  Leave 5 mm or more of free ribbon
// between the pocket and the lid slot: the 0-90 degree tilt draws 4.4 mm.
module case_ribbon_envelope() {
    translate([47.7, 2.0, board_top + 4.15]) cube([2.1, 13.4, 31.0 - board_top - 4.15]);   // riser
    translate([47.7, 2.0, 28.0]) cube([78.0 - 47.7, 22.0, 3.5]);                           // across
    translate([cable_slot[0][0] + 0.4, cable_slot[0][1] + 0.4, 28.0])                       // up through the lid
        cube([cable_slot[1][0] - cable_slot[0][0] - 0.8, cable_slot[1][1] - cable_slot[0][1] - 0.8, lid_top - 28.0]);
    translate([27.7, 0.0, board_top + cool_plate_h + 0.5])                                   // spare length
        cube([46.9 - 27.7, 17.4, wall_top - 0.5 - (board_top + cool_plate_h + 0.5)]);
}

// the space the LEDs and their legs need under the lid (checked against the Pi)
module leds5_envelope() {
    for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h - 15]) cylinder(d = 5.8, h = 15);
}

// ================================================================== lettering
// 2D panels in wall coordinates: u along the wall as read from outside, v = z.
module rr2(w, h, r) translate([r, r]) offset(r = r) square([w - 2 * r, h - 2 * r]);
module frame2(w, h, r = 2.5, t = 0.8) difference() { rr2(w, h, r); translate([t, t]) rr2(w - 2 * t, h - 2 * t, r - t); }

// front, right of the ports: the "ti" mark over T.I.E.T, in a frame (u = 0 at x = 47.5)
module tiet_panel() {
    w = 39.0; v0 = 1.5; v1 = 37.5; mark_h = 17.0;
    translate([0, v0]) frame2(w, v1 - v0);
    translate([w / 2 - mark_h * ti_mark_aspect / 2, 15.9]) scale(mark_h) thapar_ti_mark();
    translate([w / 2, 6.2]) text("T.I.E.T", size = 6.5, font = letter_font, halign = "center");
}

// back: the mentor, then the team, names left and roll numbers right, in a
// frame (u = 0 at x = 86.5, read towards -x)
module team_panel() {
    w = 88.0; v0 = 1.5; v1 = 37.5; s = 3.6; pitch = 5.31; after = 1.2;   // capitals 3.44 tall
    translate([0, v0]) frame2(w, v1 - v0);
    top = v1 - 0.8 - 1.6 - 3.44;                                          // mentor's baseline
    translate([3.4, top]) text(mentor, size = s, font = letter_font);
    for (i = [0 : len(team) - 1]) translate([0, top - after - (i + 1) * pitch]) {
        translate([3.4, 0]) text(team[i][0], size = s, font = letter_font);
        translate([w - 3.4, 0]) text(team[i][1], size = s, font = letter_font, halign = "right");
    }
}

// raised on the walls' outer faces, 0.2 mm into the wall so they fuse into one solid
module lettering() {
    translate([47.5, iy0 - wall + 0.2, 0]) rotate([90, 0, 0]) linear_extrude(letter_h + 0.2) tiet_panel();
    translate([86.5, iy1 + wall - 0.2, 0]) rotate([90, 0, 180]) linear_extrude(letter_h + 0.2) team_panel();
}

// ================================================================== base
module base() {
    difference() {
        union() {
            difference() {
                rrect(ix0 - wall, iy0 - wall, ix1 + wall, iy1 + wall, r_out, wall_top);
                translate([0, 0, floor_t]) rrect(ix0, iy0, ix1, iy1, r_out - wall, wall_top);
            }
            // Pi standoffs on the board's own holes
            for (h = pi_holes) translate([h[0], h[1], floor_t - e]) cylinder(d = 6.0, h = standoff_h + e);
            // lid screw bosses fused into the corners, thicker where the screw grips
            // (turned half a facet, so no facet corner lands on a wall face: a clean mesh)
            for (b = lid_bosses) translate([b[0], b[1], 0]) rotate(180 / $fn) {
                cylinder(d = b[2], h = wall_top);
                translate([0, 0, wall_top - 12 - (b[3] - b[2]) / 2]) cylinder(d1 = b[2], d2 = b[3], h = (b[3] - b[2]) / 2 + e);
                translate([0, 0, wall_top - 12]) cylinder(d = b[3], h = 12);
            }
            // the back bosses sit off the rounded inner corners: fill the sliver
            // between boss and corner, else it is a sealed void in the print
            for (b = lid_bosses) if (b[1] > 32)
                translate([b[0] < 40 ? ix0 - e : b[0], b[1], floor_t - e])
                    cube([b[0] < 40 ? b[0] - ix0 + e : ix1 - b[0] + e, iy1 - b[1] + e, wall_top - 12 - floor_t + 2 * e]);
            // keyhole pads (inside, under the Pi): 1.6 mm side walls, 0.8 mm roof
            for (k = keyholes) translate([k[0] - 6, k[1] - 6, floor_t - e])
                cube([key_len + 12, 12, key_lip + key_head + 0.8 - floor_t + e]);
            // ring around the power plunger (outside), so a finger finds it; it starts
            // 0.2 inside the wall, so it overlaps the wall instead of only touching it
            translate([ix0 - wall + 0.2, pwr_y, board_top + 6]) rotate([0, -90, 0])
                difference() { cylinder(d = 15, h = 1.0); translate([0, 0, -e]) cylinder(d = 12.4, h = 1.2); }
            if (lettering) lettering();
        }
        // standoff pilots (M2.5)
        for (h = pi_holes) translate([h[0], h[1], pi_pilot_z]) cylinder(d = pilot_m25, h = standoff_h + floor_t);
        // lid screw pilots (M3)
        for (b = lid_bosses) translate([b[0], b[1], wall_top - 12]) cylinder(d = pilot_m3, h = 13);

        // ---- right wall: Ethernet and the two USB stacks (microphone plugs in here)
        for (p = [eth, usb_a, usb_b])
            translate([ix1 - 1, p[0][0] - 0.5 - tol / 2, board_top + p[1][0] - 0.4 - tol / 2])
                cube([wall + 2, p[0][1] - p[0][0] + 1 + tol, p[1][1] - p[1][0] + 0.8 + tol]);
        // outside pocket: wall only 1.0 thick around the ports, so a plug seats fully
        translate([ix1 + 1.0, eth[0][0] - 2.0, board_top - 6.0])
            cube([wall, usb_b[0][1] - eth[0][0] + 4.0, 25.5]);

        // ---- front wall: USB-C power and two micro-HDMI (room for plug overmoulds)
        translate([usbc, iy0 + 1, board_top + 1.6]) rotate([90, 0, 0])
            linear_extrude(wall + 2) offset(r = 2.5) square([13.5 + tol - 5, 8.5 + tol - 5], center = true);
        for (c = [hdmi0, hdmi1]) translate([c, iy0 + 1, board_top + 1.7]) rotate([90, 0, 0])
            linear_extrude(wall + 2) offset(r = 2.0) square([11.6 + tol - 4, 7.6 + tol - 4], center = true);

        // ---- left wall: microSD slot, power-button flexure, status LED window
        translate([ix0 - wall - 1, sd_y[0] - 0.5, board_bot - 3.6]) cube([wall + 2, sd_y[1] - sd_y[0] + 1, 3.8]);
        // flexure: a tab cut free on three sides, hinged at the top
        translate([ix0 - wall - 1, 0, 0]) difference() {
            translate([0, pwr_y - 5.4, board_top - 3.5]) cube([wall + 2, 10.8, 17.0]);
            translate([-1, pwr_y - 3.8, board_top - 2.3]) cube([wall + 4, 7.6, 20]);
        }
        // the tab is thinned to 1.6 so it flexes
        translate([ix0 - wall - 1, pwr_y - 3.8, board_top - 2.3]) cube([1.8, 7.6, 14.0]);
        // status LED shows through a window
        translate([ix0 - wall - 1, led_y, board_top + 0.5]) rotate([0, 90, 0]) cylinder(d = 2.4, h = wall + 2);
        // exhaust slots facing the cooler's fins (they blow towards this end;
        // vertical slots need no bridges); the top end when on the wall
        for (j = [0 : 5]) translate([ix0 - wall - 1, 26.0 + j * 4.0, board_top + 4.0])
            cube([wall + 2, 2.0, 21.0]);
        // ---- front wall, over the ports: exhaust slots facing the heatsink fins'
        // front end (they were on the back wall until the team's names went
        // there; 8 x 2.2 x 17 mm, the same open area as the old 7 x 2 x 21)
        for (i = [0 : 7]) translate([4.0 + i * 3.6, iy0 - wall - 1, board_top + 8.0]) cube([2.2, wall + 2, 17.0]);

        // ---- underside: keyholes (closed on the inside) and foot recesses
        for (k = keyholes) translate([k[0], k[1], -e]) {
            translate([key_len, 0, 0]) cylinder(d = 8.6 + tol, h = key_lip + key_head);   // head entry
            hull() for (x = [0, key_len]) translate([x, 0, 0])                           // shank slot, round end
                cylinder(d = 4.2 + tol, h = key_lip + e);
            hull() for (x = [0, key_len]) translate([x, 0, key_lip]) cylinder(d = 8.6 + tol, h = key_head);
        }
        for (f = feet) translate([f[0], f[1], -e]) cylinder(d = 10.4, h = 0.8);
    }
    // flexure nub: reaches to 0.3 mm from the power button plunger
    translate([ix0 - e, pwr_y, board_top + pwr_z]) rotate([0, 90, 0])
        cylinder(d = 3.0, h = (-0.45 - 0.3) - ix0 + e);
}

// ================================================================== lid
module lid() {
    difference() {
        union() {
            translate([0, 0, wall_top]) rrect(ix0 - wall, iy0 - wall, ix1 + wall, iy1 + wall, r_out, lid_t);
            // locating lip inside the walls
            translate([0, 0, wall_top - 3.0]) difference() {
                rrect(ix0 + fit, iy0 + fit, ix1 - fit, iy1 - fit, max(0.6, r_out - wall - fit), 3.0 + e);
                translate([0, 0, -1]) rrect(ix0 + fit + 1.6, iy0 + fit + 1.6, ix1 - fit - 1.6, iy1 - fit - 1.6, 0.6, 5);
            }
            // buzzer cup
            translate([buzz_c[0], buzz_c[1], wall_top - buzz_h - 0.5])
                cylinder(d = buzz_d + 2 * buzz_cup_wall, h = buzz_h + 0.5 + e);
            // bracket screw bosses
            for (s = br_screws) translate([s[0], s[1], wall_top - 6]) cylinder(d = 6.5, h = 6 + e);
            // LED sleeves: the LED's 5.8 mm flange stops on the sleeve's lower end
            for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h]) cylinder(d = led_sleeve, h = led_sleeve_h + e);
        }
        for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h - 1]) cylinder(d = led_bore, h = led_sleeve_h + lid_t + 2);
        // clear the lid screw bosses of the base
        for (b = lid_bosses) translate([b[0], b[1], wall_top - 4]) cylinder(d = b[3] + 2 * fit, h = 4 + e);
        // lid screws: M3 clearance, counterbored for pan heads
        for (b = lid_bosses) translate([b[0], b[1], wall_top - 5]) {
            cylinder(d = clear_m3 + tol, h = 10);
            translate([0, 0, 5 + lid_t - 1.4]) cylinder(d = 6.4, h = 2);
        }
        // blower intake over the fan, exhaust over the fins
        translate([0, 0, wall_top - 1]) hex_field(fan_c[0] - 17, fan_c[1] - 17, fan_c[0] + 17, fan_c[1] + 17)
            translate(fan_c) circle(r = 15.5);
        translate([0, 0, wall_top - 1]) hex_field(fins_box[0][0], fins_box[0][1], fins_box[1][0], fins_box[1][1])
            translate(fins_box[0]) square([fins_box[1][0] - fins_box[0][0], fins_box[1][1] - fins_box[0][1]]);
        // buzzer: the cylinder press-fits between three crush ribs; sound holes above it
        translate([buzz_c[0], buzz_c[1], wall_top - buzz_h - 1]) cylinder(d = buzz_d + 0.6 + tol, h = buzz_h + 1 + e);
        for (a = [0 : 60 : 300]) translate([buzz_c[0] + 3.4 * cos(a), buzz_c[1] + 3.4 * sin(a), wall_top - 1])
            cylinder(d = 2.2, h = lid_t + 2);
        translate([buzz_c[0], buzz_c[1], wall_top - 1]) cylinder(d = 2.2, h = lid_t + 2);
        // camera ribbon slot under the pod
        translate([cable_slot[0][0], cable_slot[0][1], wall_top - 7])
            cube([cable_slot[1][0] - cable_slot[0][0], cable_slot[1][1] - cable_slot[0][1], 20]);
        // bracket screw pilots
        for (s = br_screws) translate([s[0], s[1], wall_top - 6.5]) cylinder(d = pilot_m3, h = 10);
        // SENTRIX, engraved 0.8 deep
        translate([label_x, 58.5, lid_top - 0.8]) linear_extrude(1)
            text("SENTRIX", size = 8.5, font = "Liberation Sans:style=Bold", halign = "center", valign = "center",
                 spacing = 1.08);
    }
    // three crush ribs hold the buzzer cylinder without screws (0.5 mm lead-in)
    translate([buzz_c[0], buzz_c[1], wall_top - buzz_h]) buzz_ribs(buzz_h);
    // three crush ribs in each LED sleeve (0.5 mm lead-in at the bottom)
    for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h + 0.5]) led_ribs(led_sleeve_h - 0.5);
}

module buzz_ribs(h) {
    bore = buzz_d + 0.6 + tol;
    for (a = [90, 210, 330]) rotate(a) translate([buzz_grip / 2, -0.4, 0]) cube([(bore - buzz_grip) / 2 + 0.3, 0.8, h]);
}

module led_ribs(h) {
    for (a = [90, 210, 330]) rotate(a) translate([led_grip / 2, -0.4, 0]) cube([(led_bore - led_grip) / 2 + 0.3, 0.8, h]);
}

// ================================================================== fork bracket
module bracket(detents = true) {
    difference() {
        union() {
            translate([0, 0, lid_top]) rrect(br_x[0], br_y[0], br_x[1], br_y[1], 3, base_t);
            for (s = [-1, 1]) {
                y = s < 0 ? br_y[0] : br_y[1] - arm_t;
                hull() {
                    translate([piv_x - 6, y, lid_top]) cube([12, arm_t, base_t]);
                    translate([piv_x, y, piv_z]) rotate([-90, 0, 0]) cylinder(r = 6, h = arm_t);
                }
            }
        }
        // pivot holes (M3 clearance)
        for (y = [br_y[0] - 1, br_y[1] - arm_t - 1]) translate([piv_x, y, piv_z]) teardrop_y(clear_m3 + tol, arm_t + 2);
        // screws to the lid (countersunk M3)
        for (s = br_screws) translate([s[0], s[1], lid_top - 1]) {
            cylinder(d = clear_m3 + tol, h = 10);
            translate([0, 0, 1 + base_t - 1.7]) cylinder(d1 = clear_m3, d2 = 6.6, h = 1.71);
        }
        translate([cable_slot[0][0], cable_slot[0][1], lid_top - 1])
            cube([cable_slot[1][0] - cable_slot[0][0], cable_slot[1][1] - cable_slot[0][1], 10]);
        // pointer for the angle scale on the pod: a notch at the top of the +y arm
        translate([piv_x, br_y[1] - 1.2, piv_z + 6]) rotate([-90, 0, 0])
            linear_extrude(2) polygon([[-1.2, -0.01], [1.2, -0.01], [0, 2.0]]);
    }
    // detent bumps on the inner face of the +y arm, one per 15 degrees; they
    // press 0.25 mm into the pod's side and click into its dimple
    if (detents) for (a = [0 : 15 : 90]) translate([piv_x, br_y[1] - arm_t, piv_z]) rotate([0, -a, 0])
        translate([4.6, 0, 0]) rotate([90, 0, 0]) cylinder(d1 = 1.6, d2 = 0.6, h = 0.5, $fn = 16);
}

module bracket_no_detents() { bracket(false); }   // for the interference checks

// ================================================================== camera pod
module pod_shell() {
    hull() for (x = [pod_back_out + pod_r, pod_front_out - pod_r], y = [-pod_y + pod_r, pod_y - pod_r],
                z = [pod_z0 + pod_r, pod_z1 - pod_r])
        translate([x, y, z]) sphere(r = pod_r, $fn = 24);
}

module pod() {
    difference() {
        union() {
            difference() {
                intersection() {   // the body ends where the back cover begins
                    pod_shell();
                    translate([pod_back_in, -50, -50]) cube([100, 100, 100]);
                }
                // cavity (open at the back, closed by pod_back)
                translate([pod_back_out - 1, -pod_y + pod_wall, pod_z0 + pod_wall])
                    cube([pod_front_in - pod_back_out + 1, 2 * (pod_y - pod_wall), pod_z1 - pod_z0 - 2 * pod_wall]);
            }
            // camera standoffs on the module's own holes (front wall -> PCB front face)
            for (h = cam_holes) translate([0, h[0], h[1]]) rotate([0, 90, 0]) cylinder(d = 4.0, h = pod_front_in + e);
            // pivot bosses inside the side walls, with a 45 degree keel towards
            // +X (down when the pod prints lens-face down: no overhang)
            for (s = [-1, 1]) translate([piv_p[0], s * (pod_y - pod_wall), piv_p[1]])
                rotate([s < 0 ? -90 : 90, 0, 0]) hull() {
                    cylinder(d = piv_boss_d, h = piv_boss_l);
                    translate([piv_boss_d / 2 * sqrt(2), 0, 0]) cylinder(d = 0.2, h = piv_boss_l);
                }
            // cover screw columns below the camera, full depth from the front
            // wall (upright in print), fused into the side walls
            for (c = cover_screws) translate([pod_back_in - e, c[0], c[1]])
                rotate([0, 90, 0]) cylinder(d = 4.4, h = pod_front_in - pod_back_in + 2 * e);
        }
        // camera screw pilots (M2, from the back through the PCB)
        for (h = cam_holes) translate([-1, h[0], h[1]]) rotate([0, 90, 0]) cylinder(d = pilot_m2, h = 6.5);
        // lens window, widening outwards for the 66 x 41 degree field of view
        rotate([0, 90, 0]) translate([0, 0, pod_front_in - 1]) cylinder(d = 7.0 + tol, h = 4);
        translate([pod_front_out - 1.3, 0, 0]) rotate([0, 90, 0]) cylinder(d1 = 7.0 + tol, d2 = 10.4, h = 1.31);
        // ribbon exit in the floor just in front of the pivot
        translate([pod_exit[0], -fpc_w / 2 - 0.8, pod_z0 - 1]) cube([pod_exit[1] - pod_exit[0], fpc_w + 1.6, pod_wall + 2]);
        // its outer back edge rounded (R 1.5): above 0 degrees of tilt the lid
        // slot pulls the ribbon backwards over that edge
        translate([pod_exit[0], 0, pod_z0]) rotate([90, 0, 0]) linear_extrude(fpc_w + 1.6, center = true)
            difference() {
                translate([-pod_exit_round, -1]) square([pod_exit_round + e, pod_exit_round + 1]);
                translate([-pod_exit_round, pod_exit_round]) circle(r = pod_exit_round, $fn = 64);
            }
        // pivot: smooth clearance holes for the through-bolt (no thread in the moving joint)
        translate([piv_p[0], 0, piv_p[1]]) rotate([90, 0, 0]) cylinder(d = clear_m3 + tol, h = 2 * pod_y + 2, center = true);
        // cover screw pilots (M2)
        for (c = cover_screws) translate([pod_back_in - 1, c[0], c[1]])
            rotate([0, 90, 0]) cylinder(d = pilot_m2, h = 6);
        // detent dimple on the +y side, on the same radius as the bracket bumps
        translate([piv_p[0] + 4.6, pod_y + e, piv_p[1]]) rotate([90, 0, 0]) cylinder(d1 = 1.8, d2 = 0.6, h = 0.6, $fn = 16);
        // angle scale on the +y side: the tick for tilt a lines up with the
        // pointer at the top of the bracket arm (long ticks at 0, 45, 90)
        for (a = [0 : 15 : 90]) translate([piv_p[0], pod_y - 0.5, piv_p[1]]) rotate([0, -(90 - a), 0])
            translate([6.8, 0, -0.35]) cube([a % 45 == 0 ? 3.4 : 2.0, 1, 0.7]);
        // small SENTRIX on the top
        translate([(pod_back_out + pod_front_out) / 2, 0, pod_z1 - 0.5]) linear_extrude(1)
            rotate(-90) text("SENTRIX", size = 3.6, font = "Liberation Sans:style=Bold", halign = "center", valign = "center");
    }
}

module pod_back() {
    difference() {
        union() {
            // plate the size of the pod's back
            intersection() {
                pod_shell();
                translate([pod_back_out - 1, -50, -50]) cube([pod_wall + 1, 100, 100]);
            }
            // locating lip inside the opening, clear of the bosses
            difference() {
                translate([pod_back_in - e, -pod_y + pod_wall + fit, pod_z0 + pod_wall + fit])
                    difference() {
                        cube([1.6, 2 * (pod_y - pod_wall - fit), pod_z1 - pod_z0 - 2 * pod_wall - 2 * fit]);
                        translate([-1, 1.2, 1.2]) cube([4, 2 * (pod_y - pod_wall - fit) - 2.4, pod_z1 - pod_z0 - 2 * pod_wall - 2 * fit - 2.4]);
                    }
                // clear of the pivot bosses and the bolt across the whole width
                translate([piv_p[0], 0, piv_p[1]]) rotate([90, 0, 0])
                    cylinder(d = piv_boss_d + 2 * fit, h = 2 * pod_y, center = true);
                for (c = cover_screws) translate([pod_back_in - 1, c[0], c[1]]) rotate([0, 90, 0])
                    cylinder(d = 4.4 + 2 * fit, h = 4);
            }
        }
        for (c = cover_screws) translate([pod_back_out - 1, c[0], c[1]]) rotate([0, 90, 0]) {
            cylinder(d = clear_m2 + tol, h = 5);
            cylinder(d = 4.4, h = 1.6);
        }
        // ventilation for the sensor
        for (z = [1.0, 5.0, 9.0]) translate([pod_back_out - 1, -7.0, z]) cube([pod_wall + 3, 14.0, 1.6]);
    }
}

// pod placed on the bracket at a tilt angle (board coordinates)
module pod_placed(a) {
    translate([piv_x, piv_y, piv_z]) rotate([0, -a, 0]) translate([-piv_p[0], 0, -piv_p[1]]) children();
}

// ================================================================== fit-test coupon
module coupon() {
    difference() {
        union() {
            translate([0, 0, 0]) rrect(0, 0, 92, 30, 3, 2.4);
            translate([75, 27.6, 0]) cube([16, wall, 15.5]);                         // USB-C test wall
            // the same pilot depths as in the case, so the real screws fit
            translate([8, 8, 0]) cylinder(d = 6.0, h = 2.4 + standoff_h);            // Pi standoff
            translate([20, 8, 0]) cylinder(d = 4.0, h = 2.4 + pod_front_in);         // camera post
            translate([32, 8, 0]) cylinder(d = 6.2, h = 2.4 + 10.0);                 // M3 boss (thinnest lid boss)
            translate([50, 15, 0]) cylinder(d = buzz_d + 2 * buzz_cup_wall, h = 2.4 + buzz_h);
            translate([62, 2, 0]) cube([10, 26, 18]);                                // USB-A window
            translate([40.5, 7.5, 0]) cylinder(d = led_sleeve, h = lid_t + led_sleeve_h);   // LED sleeve
        }
        translate([40.5, 7.5, -1]) cylinder(d = led_bore, h = 20);
        translate([8, 8, pi_pilot_z]) cylinder(d = pilot_m25, h = 10);           // 6.8 deep, as in the base
        translate([20, 8, 2.4 + pod_front_in - 5.5]) cylinder(d = pilot_m2, h = 10);   // 5.5 deep, as in the pod
        translate([32, 8, 1.6]) cylinder(d = pilot_m3, h = 12);                  // 10.8 deep (a lid screw engages 8.8)
        translate([8, 22, -1]) cylinder(d = 2.7 + tol, h = 5);       // Pi board hole size
        translate([20, 22, -1]) cylinder(d = 2.2 + tol, h = 5);      // camera board hole size
        translate([32, 22, -1]) cylinder(d = clear_m3 + tol, h = 5);
        translate([50, 15, 2.4]) cylinder(d = buzz_d + 0.6 + tol, h = 20);
        translate([61, 7.5, 3.0]) cube([12, 14.8 + tol, 7.0 + tol]);   // one USB-A port + plug body
        // the base's USB-C opening, printed upright like the base (same bridge on top):
        // the 27 W supply's plug body must slide through
        translate([83, 27.6 - 1, 2.4 + 6.0]) rotate([-90, 0, 0])
            linear_extrude(wall + 2) offset(r = 2.5) square([13.5 + tol - 5, 8.5 + tol - 5], center = true);
        translate([36, 24.5, 2.4 - 0.6]) linear_extrude(1) text("SENTRIX fit", size = 3.2,
            font = "Liberation Sans:style=Bold", halign = "center", valign = "center");
    }
    translate([50, 15, 2.4]) buzz_ribs(buzz_h - 0.5);
    translate([40.5, 7.5, lid_t]) led_ribs(led_sleeve_h - 0.5);   // as in the lid (printed the other way up)
}

// ================================================================== print orientations
module base_print()     { base(); }
module lid_print()      { translate([0, 0, lid_top]) rotate([180, 0, 0]) lid(); }
module bracket_print()  { translate([0, 0, -lid_top]) bracket(); }
module pod_print()      { rotate([0, 90, 0]) translate([-pod_front_out, 0, 0]) pod(); }    // front face down
module pod_back_print() { rotate([0, -90, 0]) translate([-pod_back_out, 0, 0]) pod_back(); } // outer face down

module assembly() {
    color("gainsboro") base();
    color("whitesmoke", 0.85) lid();
    color("dimgray") bracket();
    pod_placed(TILT) { color("dimgray") pod(); color("gray") pod_back(); camera3(); }
    pi5();
    buzzer();
    leds5();
}

module exploded() {
    color("gainsboro") base();
    translate([0, 0, 48]) pi5();
    translate([0, 0, 66]) { buzzer(); leds5(); }
    translate([0, 0, 100]) color("whitesmoke", 0.9) lid();
    translate([0, 0, 150]) color("dimgray") bracket();
    translate([0, 0, 215]) pod_placed(TILT) {
        color("dimgray") pod();
        translate([-36, 0, 0]) camera3();
        translate([-72, 0, 0]) color("gray") pod_back();
    }
}

if (PART == "assembly")    { assembly(); screws(); }
if (PART == "wall")        wall_mounted();
if (PART == "exploded")    exploded();
if (PART == "base")        base_print();
if (PART == "lid")         lid_print();
if (PART == "bracket")     bracket_print();
if (PART == "pod")         pod_print();
if (PART == "pod_back")    pod_back_print();
if (PART == "coupon")      coupon();
if (PART == "print_plate") {
    base_print();
    translate([0, 80, 0]) lid_print();
    translate([-40, 170, 0]) bracket_print();
    translate([20, 165, 0]) pod_print();
    translate([60, 165, 0]) pod_back_print();
}
