// SENTRIX enclosure, rev C (rev B + the status LED windows of rev A)
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

PART = "assembly"; // [assembly, base, lid, bracket, pod, pod_back, coupon, print_plate]
TILT = 45;         // camera tilt in the assembly view: 0 = level, 90 = straight up

/* [Fit] */
tol      = 0.20;   // added to every clearance hole and slot (raise for printers that print holes small)
fit      = 0.25;   // sliding fit per side (lid lip, pod cover, pod in the fork)
pilot_m3  = 2.50;  // M3 thread-forming pilot (use 4.0 for M3 heat-set inserts)
pilot_m25 = 2.10;  // M2.5 pilot for the Pi screws (3.5 for M2.5 heat-set inserts)
pilot_m2  = 1.60;  // M2 pilot for the camera and the pod cover
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
ix1 = 88.3;        // right: USB/Ethernet jacks end at 88.0 (3.0 past the board)
iy0 = -1.7;        // front: USB-C / micro-HDMI faces sit 1.3-1.6 past the board
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
cool_box   = [[0.65, 6.5], [64.15, 49.0]];   // footprint 63.5 x 42.5 (pins 58 x 37 apart)
cool_h     = 15.5;                            // 13.7 cooler + thermal pad + SoC, rounded up
fan_c      = [42.0, 33.5];                    // blower intake centre (30 x 30 fan)
fins_box   = [[1.5, 8.5], [25.0, 47.5]];      // heatsink fins left of the blower
// ports: [lo, hi] along the edge, [z lo, z hi] relative to the board top
eth   = [[2.25, 18.15], [-2.05, 13.70]];      // right edge, centre y 10.2
usb_a = [[21.70, 36.50], [-3.50, 16.80]];     // right edge, centre y 29.1
usb_b = [[39.60, 54.40], [-3.50, 16.80]];     // right edge, centre y 47.0
usbc  = 11.2;  hdmi0 = 25.8;  hdmi1 = 39.2;   // front edge connector centres (x)
sd_y  = [22.0, 34.0];                         // microSD on the underside, left edge
pwr_y = 18.4;  led_y = 13.3;                  // power button / status LED on the left edge
pwr_z = 1.6;                                  // button plunger centre above the board top
gpio  = [[7.1, 49.4], [57.9, 55.6]];          // 2 x 20 header
cam_fpc = [[47.2, 0.3], [56.4, 15.9]];        // CAM/DISP 0 and 1 (22-pin, vertical)

// --------------------------------------------------------- Camera Module 3 data
// Pod frame: X = optical axis (forward), Z = up, Y = sideways; origin = lens
// centre on the PCB's front face.  PCB 25 x 23.862 x 1.12, connector at the
// bottom edge on the back.  Holes dia 2.2 (M2) 2.0 from the sides, 2.0 and
// 14.5 from the bottom; lens centre 14.4 from the bottom; lens tip 6.98 in
// front of the PCB.
cam_w = 25.0;  cam_h = 23.862;  cam_t = 1.12;
cam_lens_z = 14.4;                              // from the PCB bottom edge
cam_z0 = -cam_lens_z;  cam_z1 = cam_h - cam_lens_z;   // -14.4 .. 9.462
cam_holes = [[-10.5, 2.0 - cam_lens_z], [10.5, 2.0 - cam_lens_z],
             [-10.5, 14.5 - cam_lens_z], [10.5, 14.5 - cam_lens_z]];   // (Y, Z)
cam_tip  = 6.98;        // lens tip in front of the PCB front face
cam_back = 3.2;         // connector behind the PCB back face
fpc_w    = 16.0;        // 15-pin ribbon at the camera end (the Pi end is 11.5)

// pod geometry (pod frame)
pod_wall = 2.0;
pod_front_out = cam_tip + 0.6;                  // lens tip 0.6 behind the front face
pod_front_in  = pod_front_out - pod_wall;       // standoffs reach from here to the PCB
pod_back_out  = -9.5;
pod_back_in   = pod_back_out + pod_wall;
pod_y  = cam_w / 2 + 0.6 + pod_wall;            // half width 15.1
pod_z1 = cam_z1 + 0.6 + pod_wall;               // top 12.06
pod_z0 = cam_z0 - 4.5 - pod_wall;               // bottom -20.9 (ribbon bend below the PCB)
pod_r  = 3.0;
piv_p  = [-4.5, -18.0];                         // pivot (X, Z) in the pod frame: low, just in front of the back cover
piv_boss_d = 5.6;                               // fits inside the pod's back-bottom corner

// fork bracket on the lid (board coordinates)
piv_x = 72.0;  piv_y = 15.0;
base_t = 3.0;
piv_h  = 7.0;                                   // pivot above the bracket base (the pod's back corner swings 5.8 below it)
piv_z  = lid_top + base_t + piv_h;
arm_t  = 3.5;
arm_gap = pod_y + fit;                          // inner faces of the arms
br_x = [piv_x - 14, piv_x + 14];                // base plate along x (arms span piv_x +-6)
br_y = [piv_y - arm_gap - arm_t, piv_y + arm_gap + arm_t];
// screws beside the arms (x) and far enough in (y) that their bosses under the
// lid stay clear of the front wall
br_screws = [[br_x[0] + 3, br_y[0] + 8], [br_x[1] - 4, br_y[0] + 8],
             [br_x[0] + 3, br_y[1] - 6], [br_x[1] - 4, br_y[1] - 6]];
cable_slot = [[piv_x - 1.0, piv_y - 9.0], [piv_x + 2.5, piv_y + 9.0]];   // lid + bracket

// buzzer: a cylinder with no screw holes, held in a cup under the lid
buzz_d = 12.0;  buzz_h = 9.5;  buzz_pcb = 1.6;
buzz_c = [76.0, 43.0];
buzz_cup_wall = 1.6;

// status LEDs (5 mm, pushed into sleeves under the lid from inside; the code
// drives GPIO 5 / 12 / 13 as green = working, amber = attention, red = alarm)
leds = [[63.0, 58.5], [69.5, 58.5], [76.0, 58.5]];
led_d = 5.0;  led_sleeve = 7.4;  led_sleeve_h = 5.0;
label_x = 30.5;                                 // SENTRIX lettering centre, left of the LEDs

// lid screws (board coordinates), fused into the corners of the base
lid_bosses = [[-1.6, -0.7, 5.2], [86.6, -0.6, 5.2], [-0.5, 63.0, 6.0], [85.3, 63.0, 6.0]];
// wall keyholes: final screw position; the slot runs +x to the entry hole
keyholes = [[22.0, 61.5], [64.0, 61.5]];
key_len = 8.0;
feet = [[3.0, 3.0], [83.0, 3.0], [3.0, 58.0], [83.0, 58.0]];

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
                rotate(30) hex_hole(af, h);
        linear_extrude(h) children();
    }
}

// ================================================================== components (ghosts)
module pi5() {
    color("seagreen") translate([0, 0, board_bot]) rrect(0, 0, 85, 56, 3, pcb_t);
    // cooler
    color("silver") translate([0, 0, board_top]) box([cool_box[0][0], cool_box[0][1], 0],
                                                   [cool_box[1][0], cool_box[1][1], cool_h]);
    color("dimgray") translate([fan_c[0], fan_c[1], board_top + cool_h - 0.1]) cylinder(d = 26, h = 0.5);
    // ports
    color("lightgray") {
        for (p = [eth, usb_a, usb_b])
            box([85 - 17, p[0][0], board_top + p[1][0]], [88.0, p[0][1], board_top + p[1][1]]);
        translate([usbc - 4.47, -1.3, board_top]) cube([8.94, 7.5, 3.2]);
        for (c = [hdmi0, hdmi1]) translate([c - 3.25, -1.6, board_top]) cube([6.5, 7.5, 3.4]);
    }
    color("black") translate([gpio[0][0], gpio[0][1], board_top]) cube([50.8, 6.2, 8.5]);
    // microSD card (sticks out 2.5 under the left edge)
    color("royalblue") translate([-2.5, sd_y[0] + 0.5, board_bot - 1.2]) cube([14, 11, 1.0]);
    // power button plunger
    color("red") translate([-0.45, pwr_y - 1.5, board_top]) cube([2, 3, 3.2]);
}

// the space the Pi needs (checked against the case: must not intersect)
module pi5_envelope() {
    translate([0, 0, board_bot + e]) rrect(0, 0, 85, 56, 3, pcb_t - 2 * e);
    translate([0, 0, board_top]) box([cool_box[0][0], cool_box[0][1], 0], [cool_box[1][0], cool_box[1][1], cool_h]);
    for (p = [eth, usb_a, usb_b])
        box([85 - 17, p[0][0], board_top + p[1][0]], [88.0, p[0][1], board_top + p[1][1]]);
    translate([usbc - 4.47, -1.3, board_top]) cube([8.94, 7.5, 3.2]);
    for (c = [hdmi0, hdmi1]) translate([c - 3.25, -1.6, board_top]) cube([6.5, 7.5, 3.4]);
    // GPIO header with DuPont plugs on it (17 mm above the board)
    box([gpio[0][0] - 0.6, gpio[0][1] - 1.2, board_top], [gpio[1][0] + 0.6, gpio[1][1] + 1.2, board_top + 17.0]);
    translate([-2.5, sd_y[0] + 0.5, board_bot - 1.2]) cube([14, 11, 1.0]);
    translate([-0.45, pwr_y - 1.5, board_top]) cube([2, 3, 3.2]);
    // cooler push-pin tips under the board
    for (p = cool_pins) translate([p[0], p[1], board_bot - 2.5]) cylinder(d = 3.2, h = 2.5);
}

module camera3() {   // in the pod frame
    color("seagreen") translate([-cam_t, -cam_w / 2, cam_z0]) cube([cam_t, cam_w, cam_h]);
    color("dimgray") translate([0, -5.4, -5.4]) cube([2.75, 10.8, 10.8]);
    color("black") rotate([0, 90, 0]) cylinder(d = 5.75, h = cam_tip);
    color("white") translate([-cam_t - cam_back, -10, cam_z0]) cube([cam_back, 20, 5.71]);
}

module camera3_envelope() {
    translate([-cam_t + e, -cam_w / 2, cam_z0]) cube([cam_t - 2 * e, cam_w, cam_h]);
    translate([0, -5.4, -5.4]) cube([3.875, 10.8, 10.8]);
    rotate([0, 90, 0]) cylinder(d = 5.75, h = cam_tip);
    translate([-cam_t - cam_back, -10, cam_z0]) cube([cam_back, 20, 5.71]);
}

module buzzer() {   // module board + cylinder, as seated in the lid cup
    color("black") translate([buzz_c[0], buzz_c[1], wall_top - buzz_h]) cylinder(d = buzz_d, h = buzz_h);
    color("navy") translate([buzz_c[0] - 9.25, buzz_c[1] - 7.5, wall_top - buzz_h - buzz_pcb])
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

// the space the LEDs and their legs need under the lid (checked against the Pi)
module leds5_envelope() {
    for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h - 15]) cylinder(d = 5.8, h = 15);
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
            // lid screw bosses fused into the corners
            for (b = lid_bosses) translate([b[0], b[1], 0]) cylinder(d = b[2], h = wall_top);
            // keyhole pads (inside, behind the GPIO edge)
            for (k = keyholes) translate([k[0] - 6, k[1] - 4.5, floor_t - e]) cube([key_len + 12, 9, 2.4]);
            // ring around the power plunger (outside), so a finger finds it
            translate([ix0 - wall, pwr_y, board_top + 6]) rotate([0, -90, 0])
                difference() { cylinder(d = 15, h = 0.8); translate([0, 0, -e]) cylinder(d = 12.4, h = 1); }
        }
        // standoff pilots (M2.5)
        for (h = pi_holes) translate([h[0], h[1], floor_t]) cylinder(d = pilot_m25, h = standoff_h + 1);
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
        translate([ix0 - wall - 1, led_y, board_top + 0.8]) rotate([0, 90, 0]) cylinder(d = 2.4, h = wall + 2);
        // exhaust slots (vertical: no bridges)
        for (j = [0 : 5]) translate([ix0 - wall - 1, 37.0 + j * 4.0, board_top + 4.0])
            cube([wall + 2, 2.0, 21.0]);
        // ---- back wall: exhaust slots behind the heatsink fins
        for (i = [0 : 6]) translate([4.0 + i * 4.0, iy1 - 1, board_top + 4.0]) cube([2.0, wall + 2, 21.0]);

        // ---- underside: keyholes (closed on the inside) and foot recesses
        for (k = keyholes) translate([k[0], k[1], -e]) {
            translate([key_len, 0, 0]) cylinder(d = 8.6 + tol, h = 4.0);            // head entry
            translate([0, -2.1 - tol / 2, 0]) cube([key_len, 4.2 + tol, 1.6]);      // shank slot
            hull() for (x = [0, key_len]) translate([x, 0, 1.6]) cylinder(d = 8.6 + tol, h = 2.4);
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
        for (l = leds) translate([l[0], l[1], wall_top - led_sleeve_h - 1]) cylinder(d = led_d + tol, h = led_sleeve_h + lid_t + 2);
        // clear the lid screw bosses of the base
        for (b = lid_bosses) translate([b[0], b[1], wall_top - 4]) cylinder(d = b[2] + 2 * fit, h = 4 + e);
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
    // three crush ribs hold the buzzer cylinder without screws
    for (a = [90, 210, 330]) translate([buzz_c[0] + (buzz_d / 2 + 0.3 + tol / 2) * cos(a),
                                        buzz_c[1] + (buzz_d / 2 + 0.3 + tol / 2) * sin(a), wall_top - buzz_h - 0.5])
        rotate(a) translate([-0.55, -0.6, 0]) cube([0.8, 1.2, buzz_h + 0.5]);
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
            // pivot bosses inside the side walls
            for (s = [-1, 1]) translate([piv_p[0], s * (pod_y - pod_wall), piv_p[1]])
                rotate([s < 0 ? -90 : 90, 0, 0]) cylinder(d = piv_boss_d, h = 3.5);
            // cover screw bosses (top corners)
            for (s = [-1, 1]) translate([pod_back_in - e, s * 11.6, pod_z1 - pod_wall - 2.2])
                rotate([0, 90, 0]) cylinder(d = 4.4, h = 5.0);
        }
        // camera screw pilots (M2, from the back through the PCB)
        for (h = cam_holes) translate([-1, h[0], h[1]]) rotate([0, 90, 0]) cylinder(d = pilot_m2, h = 6.5);
        // lens window, widening outwards for the 66 x 41 degree field of view
        rotate([0, 90, 0]) translate([0, 0, pod_front_in - 1]) cylinder(d = 7.0 + tol, h = 4);
        translate([pod_front_out - 1.3, 0, 0]) rotate([0, 90, 0]) cylinder(d1 = 7.0 + tol, d2 = 10.4, h = 1.31);
        // ribbon exit right under the pivot: hardly moves when the pod tilts
        translate([pod_back_in - e, -fpc_w / 2 - 0.8, pod_z0 - 1]) cube([3.0, fpc_w + 1.6, pod_wall + 2]);
        // pivot pilots (M3)
        for (s = [-1, 1]) translate([piv_p[0], s * (pod_y + 1), piv_p[1]])
            rotate([s < 0 ? -90 : 90, 0, 0]) cylinder(d = pilot_m3, h = 7);
        // cover screw pilots (M2)
        for (s = [-1, 1]) translate([pod_back_in - 1, s * 11.6, pod_z1 - pod_wall - 2.2])
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
                for (s = [-1, 1]) {
                    translate([piv_p[0], s * (pod_y - pod_wall - 4), piv_p[1]]) rotate([90, 0, 0])
                        cylinder(d = piv_boss_d + 2 * fit, h = 8, center = true);
                    translate([pod_back_in - 1, s * 11.6, pod_z1 - pod_wall - 2.2]) rotate([0, 90, 0])
                        cylinder(d = 4.4 + 2 * fit, h = 4);
                }
            }
        }
        for (s = [-1, 1]) translate([pod_back_out - 1, s * 11.6, pod_z1 - pod_wall - 2.2]) rotate([0, 90, 0]) {
            cylinder(d = clear_m2 + tol, h = 5);
            cylinder(d = 4.4, h = 1.6);
        }
        // notch so the ribbon passes the cover
        translate([pod_back_out - 1, -fpc_w / 2 - 0.8, pod_z0 - 1]) cube([pod_wall + 3, fpc_w + 1.6, pod_wall + 2.2]);
        // ventilation for the sensor
        for (z = [-6, -2, 2, 6]) translate([pod_back_out - 1, -8, z]) cube([pod_wall + 3, 16, 1.6]);
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
            translate([0, 0, 0]) rrect(0, 0, 74, 30, 3, 2.4);
            translate([8, 8, 0]) cylinder(d = 6.0, h = 2.4 + standoff_h);            // Pi standoff
            translate([20, 8, 0]) cylinder(d = 4.0, h = 2.4 + 5.0);                  // camera standoff
            translate([32, 8, 0]) cylinder(d = 6.0, h = 2.4 + 8.0);                  // M3 boss
            translate([50, 15, 0]) cylinder(d = buzz_d + 2 * buzz_cup_wall, h = 2.4 + buzz_h);
            translate([62, 2, 0]) cube([10, 26, 18]);                                // USB-A window
            translate([40.5, 7.5, 0]) cylinder(d = led_sleeve, h = lid_t + led_sleeve_h);   // LED sleeve
        }
        translate([40.5, 7.5, -1]) cylinder(d = led_d + tol, h = 20);
        translate([8, 8, 2.4]) cylinder(d = pilot_m25, h = 10);
        translate([20, 8, 2.4]) cylinder(d = pilot_m2, h = 10);
        translate([32, 8, 2.4]) cylinder(d = pilot_m3, h = 10);
        translate([8, 22, -1]) cylinder(d = 2.7 + tol, h = 5);       // Pi board hole size
        translate([20, 22, -1]) cylinder(d = 2.2 + tol, h = 5);      // camera board hole size
        translate([32, 22, -1]) cylinder(d = clear_m3 + tol, h = 5);
        translate([50, 15, 2.4]) cylinder(d = buzz_d + 0.6 + tol, h = 20);
        translate([61, 7.5, 3.0]) cube([12, 14.8 + tol, 7.0 + tol]);   // one USB-A port + plug body
        translate([36, 24.5, 2.4 - 0.6]) linear_extrude(1) text("SENTRIX fit", size = 3.2,
            font = "Liberation Sans:style=Bold", halign = "center", valign = "center");
    }
    for (a = [90, 210, 330]) translate([50 + (buzz_d / 2 + 0.3 + tol / 2) * cos(a), 15 + (buzz_d / 2 + 0.3 + tol / 2) * sin(a), 2.4])
        rotate(a) translate([-0.55, -0.6, 0]) cube([0.8, 1.2, buzz_h]);
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
    pi5();
    translate([0, 0, 28]) { color("whitesmoke", 0.9) lid(); buzzer(); leds5(); }
    translate([0, 0, 50]) color("dimgray") bracket();
    translate([0, 0, 72]) pod_placed(TILT) { color("dimgray") pod(); camera3(); }
    translate([-26, 0, 72]) pod_placed(TILT) color("gray") pod_back();
}

if (PART == "assembly")    assembly();
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
