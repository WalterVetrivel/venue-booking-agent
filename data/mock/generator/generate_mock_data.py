"""Generate the mock dataset for the venue booking agent prototype.

Outputs (under --out, default ./data/mock):
  venues.csv, faculty.csv
  timetables/*.pdf + timetables/lab_slots_truth.csv
  drive/circulars/*.pdf, drive/reports/*.pdf  (upload these to the Drive folder)
  ground_truth/events.csv                     (what the extractor should recover)
  README.md

Everything is fictional except the institution letterhead text and the venue
names, which follow the real samples. Run:  python generate_mock_data.py --out ../
Requires: reportlab
"""

import argparse
import csv
import datetime as dt
import json
import random
from collections import defaultdict
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SEED = 2026
TODAY = dt.date(2026, 10, 20)
COLLEGE = "KNOWLEDGE INSTITUTE OF TECHNOLOGY"

# --------------------------------------------------------------------------
# Venues
# --------------------------------------------------------------------------

BASE_SW = ["Windows 11", "MS Office", "Google Chrome", "VS Code", "Python 3.12", "JDK 17", "Eclipse", "Git", "GCC/G++"]
LAB_SW = {
    "CC1": ["MySQL Workbench", "Node.js", "IntelliJ IDEA", "Anaconda"],
    "CC2": ["MySQL Workbench", "Node.js", "MongoDB Compass", "Selenium"],
    "CC3": ["Cisco Packet Tracer", "Wireshark", "GNS3"],
    "CC4": ["Cisco Packet Tracer", "VirtualBox", "Wireshark", "Nmap", "Docker Desktop"],
    "CC5": ["Anaconda", "Jupyter", "TensorFlow", "RStudio", "Power BI Desktop"],
    "CC6": ["Visual Studio 2022", ".NET 8 SDK", "SQL Server Express", "MySQL Workbench"],
    "CC7": ["Arduino IDE", "Proteus", "Keil uVision"],
    "CC8": ["Android Studio", "Flutter SDK", "Arduino IDE", "Node.js"],
    "CC9": ["Visual Studio 2022", "MySQL Workbench", "Selenium", "JMeter", "Postman", "Node.js"],
    "CC10": ["MATLAB", "Power BI Desktop", "Tableau Public", "Anaconda", "VirtualBox", "Docker Desktop"],
    "CC11": ["Docker Desktop", "AWS CLI", "Terraform", "VirtualBox", "Node.js"],
}

# KIOT faculty (CSE department, as given by the admin). Everyone at KIOT in the mock data is drawn from this list;
# only outside guests (industry, other institutions, government) are made up. Emails are placeholders.
KIOT_FACULTY = [
    ("Dr. P. Rajendran", "Prof. & HoD"), ("Dr. V. Kumar", "Professor"), ("Dr. R. Kumar", "Professor"),
    ("Dr. S. Sakthivel", "Assoc. Prof."), ("Dr. P. Vijayalakshmi", "Assoc. Prof."), ("Dr. M. Sakthivel", "Assoc. Prof."),
    ("Dr. I. Rajesh", "Asst. Prof."), ("Mr. K. Ravikumar", "Asst. Prof."), ("Mr. T. Karthikeyan", "Asst. Prof."),
    ("Dr. P. Malarvizhi", "Asst. Prof."), ("Mrs. K. Saranya", "Asst. Prof."), ("Mr. M. Senthilkumar", "Asst. Prof."),
    ("Mrs. R. Saranya", "Asst. Prof."), ("Mr. A. Sekar", "Asst. Prof."), ("Mr. P. Nareshkumar", "Asst. Prof."),
    ("Mr. P. Sasikumar", "Asst. Prof."), ("Mr. G. Babu", "Asst. Prof."), ("Mr. K. Boopathi", "Asst. Prof."),
    ("Mr. K. Praveen", "Asst. Prof."), ("Mr. R. Sivaguru", "Asst. Prof."), ("Mrs. N. Subha", "Asst. Prof."),
    ("Mrs. D. Ramya", "Asst. Prof."), ("Mr. P. Ganeshan", "Asst. Prof."), ("Mrs. S. Deepa", "Asst. Prof."),
    ("Dr. M. Lakshaga Jyothi", "Asst. Prof."), ("Mrs. V. SathiyaPriya", "Asst. Prof."), ("Mrs. B. Ashwini", "Asst. Prof."),
    ("Mrs. K. Gowthami", "Asst. Prof."), ("Mr. M. K. Sampath", "Asst. Prof."), ("Mr. T. Palani Raja", "Asst. Prof."),
    ("Mrs. R. Pushpalatha", "Asst. Prof."), ("Mrs. E. Kiruthika", "Asst. Prof."), ("Mrs. M. Saranya", "Asst. Prof."),
    ("Ms. P. Vikneshwary", "Asst. Prof."), ("Mrs. G. Eswari @ Petchiammal", "Asst. Prof."),
    ("Mr. S. Walter Vetrivel", "Asst. Prof."), ("Mr. C. Siva", "Asst. Prof."), ("Mrs. S. Sowndarya", "Asst. Prof."),
    ("Mrs. G. Pavithra", "Asst. Prof."), ("Mrs. S. Manju", "Asst. Prof."), ("Mrs. V. Anitha", "Asst. Prof."),
    ("Mr. R. Krishnakumar", "Asst. Prof."), ("Ms. A. Prabha", "Asst. Prof."), ("Mrs. T. Sowndharya", "Asst. Prof."),
    ("Mr. C. Aravindan", "Asst. Prof."),
]
ADMIN_NAME = "Mr. S. Walter Vetrivel"
SHORT_DESIG = {"Prof. & HoD": "Prof & HoD", "Professor": "Prof", "Assoc. Prof.": "ASP", "Asst. Prof.": "AP"}


def email_of(name):
    """Placeholder address: surname.initials@example.com, e.g. kumar.v@example.com."""
    parts = name.split(" @ ")[0].split()[1:]
    initials = "".join(p[0] for p in parts if p.endswith("."))
    surname = "".join(p for p in parts if not p.endswith("."))
    return f"{surname.lower()}.{initials.lower()}@example.com"


# Who approves each venue. Placeholders from the faculty list until the real in-charges are known.
IN_CHARGES = {k: (n, email_of(n)) for k, n in {
    "labs_a": "Mr. K. Ravikumar", "labs_b": "Mr. T. Karthikeyan", "A Block SH": "Dr. I. Rajesh",
    "CSE PG SH": "Dr. P. Rajendran", "D Block SH": "Dr. M. Sakthivel", "E Block SH": "Dr. S. Sakthivel",
    "MBA SH": "Dr. P. Vijayalakshmi", "COE": "Dr. P. Malarvizhi"}.items()}


def build_venues():
    rows = []
    for i in range(1, 12):
        code = f"CC{i}"
        cap = 60 if i <= 2 else 30
        ic = IN_CHARGES["labs_a" if i <= 6 else "labs_b"]
        rows.append({
            "code": code, "name": f"{code} Laboratory", "type": "computer_lab", "building": "",
            "capacity": cap, "n_computers": cap, "byod": False,
            "equipment": "projector;AC;LAN;Wi-Fi",
            "software": ";".join(BASE_SW + LAB_SW[code]),
            "typical_use": "academic labs;training;assessments;value-added courses",
            "aliases": f"{code};{code} Lab;{code} Laboratory",
            "in_charge_name": ic[0], "in_charge_email": ic[1],
        })
    halls = [
        ("A Block SH", "A Block Seminar Hall", "A Block", 100, "A Block SH;A-Block Seminar Hall;Seminar Hall, A Block;A Block Seminar Hall"),
        ("CSE PG SH", "CSE PG Seminar Hall", "CSE Block", 70, "CSE PG SH;CSE PG Seminar Hall;PG Seminar Hall (CSE)"),
        ("D Block SH", "D Block Seminar Hall", "D Block", 300, "D Block SH;D-Block Seminar Hall;Seminar Hall, D Block;D Block Seminar Hall"),
        ("E Block SH", "E Block Seminar Hall", "E Block", 350, "E Block SH;E-Block Seminar Hall;Seminar Hall, E Block;E Block Seminar Hall"),
        ("MBA SH", "MBA Seminar Hall", "MBA Block", 150, "MBA SH;MBA Seminar Hall;Seminar Hall, MBA Block"),
    ]
    for code, name, bld, cap, aliases in halls:
        ic = IN_CHARGES[code]
        extra = ";stage" if cap >= 300 else ""
        rows.append({
            "code": code, "name": name, "type": "seminar_hall", "building": bld,
            "capacity": cap, "n_computers": 0, "byod": False,
            "equipment": "projector;PA system;AC;podium;Wi-Fi" + extra, "software": "",
            "typical_use": "FDPs;inaugurations;seminars;guest lectures;Executive Chairman/Principal/HOD addresses",
            "aliases": aliases, "in_charge_name": ic[0], "in_charge_email": ic[1],
        })
    for i in range(1, 4):
        code = f"COE{i}"
        ic = IN_CHARGES["COE"]
        rows.append({
            "code": code, "name": f"Centre of Excellence Lab {i}", "type": "coe_lab", "building": "Centre of Excellence",
            "capacity": 20, "n_computers": 0, "byod": True,
            "equipment": "projector;Wi-Fi;power sockets at every seat", "software": "",
            "typical_use": "placement training;in-house contests;hackathons",
            "aliases": f"{code};{code} Lab;Centre of Excellence {i};CoE Lab {i}",
            "in_charge_name": ic[0], "in_charge_email": ic[1],
        })
    return rows


# --------------------------------------------------------------------------
# People: KIOT faculty from the list above; outside guests are fictional
# --------------------------------------------------------------------------

FIRST = ["Arun", "Priya", "Karthik", "Divya", "Suresh", "Lakshmi", "Vignesh", "Meena", "Rajesh", "Kavya", "Manoj",
         "Deepa", "Ganesh", "Nithya", "Prakash", "Revathi", "Saravanan", "Sangeetha", "Vijay", "Yamuna", "Bala",
         "Harini", "Dinesh", "Keerthana", "Mohan", "Pavithra", "Ramesh", "Swathi", "Thiru", "Uma"]
INIT = list("ABCDEGJKMNPRSTV")
DEPTS = ["CSE", "IT", "AI&DS", "CSBS", "ECE", "EEE", "MECH", "MBA"]


def person(rng, title=None):
    name = rng.choice(FIRST)
    female = name in {"Priya", "Divya", "Lakshmi", "Meena", "Kavya", "Deepa", "Nithya", "Revathi", "Sangeetha",
                      "Yamuna", "Harini", "Keerthana", "Pavithra", "Swathi", "Uma"}
    t = title or ("Mrs." if female else "Mr.")
    return f"{t} {rng.choice(INIT)}. {name}"


def kiot_person(rng):
    """Someone from the faculty list, with their designation. Uses the same two draws as person(), so the rest of
    the generated data stays the same."""
    i = FIRST.index(rng.choice(FIRST)) * len(INIT) + INIT.index(rng.choice(INIT))
    name, desig = KIOT_FACULTY[i % len(KIOT_FACULTY)]
    return f"{name}, {SHORT_DESIG[desig]}/CSE"


def build_faculty(rng):
    rows = [{"email": "ADMIN_EMAIL (from .env)", "name": ADMIN_NAME, "department": "CSE", "role": "admin"}]
    in_charge = {n for n, _ in IN_CHARGES.values()}
    for _ in range(len(DEPTS) * 3):  # the draws the old made-up list used, so the rest stays the same
        person(rng)
    for name, _ in KIOT_FACULTY:
        if name != ADMIN_NAME:
            rows.append({"email": email_of(name), "name": name, "department": "CSE",
                         "role": "in_charge" if name in in_charge else "faculty"})
    return rows


# --------------------------------------------------------------------------
# Timetables
# --------------------------------------------------------------------------

PERIODS = ["09.00-09.50", "09.50-10.40", "10.55-11.45", "11.45-12.35", "01.25-02.10", "02.10-02.55", "03.10-03.55", "03.55-04.40"]
PERIOD_24 = [("09:00", "09:50"), ("09:50", "10:40"), ("10:55", "11:45"), ("11:45", "12:35"),
             ("13:25", "14:10"), ("14:10", "14:55"), ("15:10", "15:55"), ("15:55", "16:40")]
BLOCKS = [(0, 1), (2, 3), (4, 5), (6, 7)]  # two-period lab blocks
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

# (acronym, name, software needed or None for theory)
CURRICULUM = {
    ("CSE", "II"): {"theory": [("DM", "Discrete Mathematics"), ("DPCO", "Digital Principles and Computer Organization"), ("OS", "Operating Systems"), ("ESE", "Environmental Science and Engineering")],
                     "labs": [("DS", "Data Structures", "GCC/G++"), ("DBMS", "Database Management Systems", "MySQL Workbench"), ("OOP", "Object Oriented Programming using Java", "JDK 17")]},
    ("CSE", "III"): {"theory": [("CN", "Computer Networks"), ("TOC", "Theory of Computation"), ("E&ST", "Entrepreneurship and Start-ups"), ("IC", "Indian Constitution")],
                      "labs": [("C#&.N", "C# & .NET", "Visual Studio 2022"), ("ES&IOT", "Embedded Systems and IoT", "Arduino IDE"), ("CNL", "Computer Networks Laboratory", "Cisco Packet Tracer")]},
    ("CSE", "IV"): {"theory": [("CC", "Cloud Computing"), ("CS", "Cyber Security"), ("PPM", "Principles of Project Management"), ("PE-V", "Professional Elective V")],
                     "labs": [("CSL", "Cyber Security Laboratory", "VirtualBox"), ("CCL", "Cloud Computing Laboratory", "Docker Desktop"), ("ML", "Machine Learning", "Anaconda")]},
    ("IT", "II"): {"theory": [("DM", "Discrete Mathematics"), ("OS", "Operating Systems"), ("SE", "Software Engineering"), ("ESE", "Environmental Science and Engineering")],
                    "labs": [("DS", "Data Structures", "GCC/G++"), ("DBMS", "Database Management Systems", "MySQL Workbench"), ("FSD", "Full Stack Development", "Node.js")]},
    ("IT", "III"): {"theory": [("CN", "Computer Networks"), ("DAA", "Design and Analysis of Algorithms"), ("E&ST", "Entrepreneurship and Start-ups"), ("IC", "Indian Constitution")],
                     "labs": [("MAD", "Mobile Application Development", "Android Studio"), ("ST", "Software Testing", "Selenium"), ("CNL", "Computer Networks Laboratory", "Cisco Packet Tracer")]},
    ("AI&DS", "II"): {"theory": [("PS", "Probability and Statistics"), ("AI", "Artificial Intelligence"), ("OS", "Operating Systems"), ("ESE", "Environmental Science and Engineering")],
                       "labs": [("DS", "Data Structures", "GCC/G++"), ("DBMS", "Database Management Systems", "MySQL Workbench"), ("DSP", "Data Science using Python", "Anaconda")]},
    ("AI&DS", "III"): {"theory": [("DL", "Deep Learning"), ("BDA", "Big Data Analytics"), ("E&ST", "Entrepreneurship and Start-ups"), ("IC", "Indian Constitution")],
                        "labs": [("MLL", "Machine Learning Laboratory", "Anaconda"), ("DV", "Data Visualization", "Power BI Desktop"), ("CCL", "Cloud Computing Laboratory", "Docker Desktop")]},
    ("CSBS", "II"): {"theory": [("CBM", "Computational Business Models"), ("OS", "Operating Systems"), ("FA", "Financial Accounting"), ("ESE", "Environmental Science and Engineering")],
                      "labs": [("DBMS", "Database Management Systems", "MySQL Workbench"), ("OOP", "Object Oriented Programming using Java", "JDK 17"), ("BA", "Business Analytics", "Power BI Desktop")]},
    ("CSBS", "III"): {"theory": [("CN", "Computer Networks"), ("SE", "Software Engineering"), ("E&ST", "Entrepreneurship and Start-ups"), ("IC", "Indian Constitution")],
                       "labs": [("ML", "Machine Learning", "Anaconda"), ("ST", "Software Testing", "Selenium"), ("CSL", "Cyber Security Laboratory", "VirtualBox")]},
}
CLASSES = [("CSE", y, s) for y in ("II", "III", "IV") for s in "ABCD"] + \
          [("IT", y, s) for y in ("II", "III") for s in "AB"] + \
          [("AI&DS", y, s) for y in ("II", "III") for s in "AB"] + \
          [("CSBS", y, "A") for y in ("II", "III")]
SEM = {"II": "III", "III": "V", "IV": "VII"}
DEPT_FULL = {"CSE": "Computer Science and Engineering", "IT": "Information Technology",
             "AI&DS": "Artificial Intelligence and Data Science", "CSBS": "Computer Science and Business Systems"}


def labs_with(sw, venues):
    return [v["code"] for v in venues if v["type"] == "computer_lab" and sw in v["software"].split(";")]


def schedule_timetables(rng, venues):
    cap = {v["code"]: v["capacity"] for v in venues}
    busy = defaultdict(set)  # (day, block) -> labs in use
    plans = {}
    for cls in CLASSES:
        dept, year, sec = cls
        cur = CURRICULUM[(dept, year)]
        labs = cur["labs"]
        # three sessions: (A,B) swapped twice, then C for whole class
        sessions = [(labs[0], labs[1]), (labs[1], labs[0]), (labs[2], None)]
        grid = [[None] * 8 for _ in DAYS]
        placed = []
        used_days = set()
        for b1, b2 in sessions:
            options = [(d, bl) for d in range(5) for bl in range(4) if d not in used_days]
            rng.shuffle(options)
            done = False
            for d, bl in options:
                free = lambda c: c not in busy[(d, bl)]
                if b2 is None:
                    big = [c for c in labs_with(b1[2], venues) if cap[c] >= 60 and free(c)]
                    small = [c for c in labs_with(b1[2], venues) if cap[c] < 60 and free(c)]
                    if big:
                        alloc = [("all", b1, big[0])]
                    elif len(small) >= 2:
                        alloc = [("B1", b1, small[0]), ("B2", b1, small[1])]
                    else:
                        continue
                else:
                    la = [c for c in labs_with(b1[2], venues) if free(c)]
                    lb = [c for c in labs_with(b2[2], venues) if free(c)]
                    pick = next(((a, b) for a in la for b in lb if a != b), None)
                    if not pick:
                        continue
                    alloc = [("B1", b1, pick[0]), ("B2", b2, pick[1])]
                for _, _, lab in alloc:
                    busy[(d, bl)].add(lab)
                p0, p1 = BLOCKS[bl]
                grid[d][p0] = grid[d][p1] = ("LAB", alloc)
                placed.append((d, bl, alloc))
                used_days.add(d)
                done = True
                break
            if not done:
                raise RuntimeError(f"could not place lab for {cls}")
        # theory fill
        pool = [t[0] for t in cur["theory"]] * 5 + [l[0] + " (T)" for l in labs] + ["AS", "CS-I", "LIB", "HOD/MENTOR"]
        rng.shuffle(pool)
        k = 0
        for d in range(5):
            for p in range(8):
                if grid[d][p] is None:
                    grid[d][p] = ("TH", pool[k % len(pool)])
                    k += 1
        plans[cls] = {"grid": grid, "placed": placed}
    return plans


def cell_text(c):
    kind, val = c
    if kind == "TH":
        return val
    parts = []
    for batch, subj, lab in val:
        parts.append(f"{subj[0]} LAB ({lab})" if batch == "all" else f"{subj[0]} ({batch}, {lab})")
    return " / ".join(parts)


def render_timetable(path, cls, plan, rng):
    dept, year, sec = cls
    cur = CURRICULUM[(dept, year)]
    st = ParagraphStyle("c", fontName="Times-Roman", fontSize=8, leading=9, alignment=TA_CENTER)
    stb = ParagraphStyle("cb", parent=st, fontName="Times-Bold", fontSize=9, leading=11)
    P = lambda t, s=st: Paragraph(t.replace("&", "&amp;"), s)
    advisor = kiot_person(rng)
    head = [
        [P(f"{COLLEGE.title()}, Salem-637504<br/>(An Autonomous Institution)", stb)] + [""] * 10 + [P("Version<br/>1")],
        [P(f"Department of {DEPT_FULL[dept]}", stb)] + [""] * 10 + [P("Date<br/>19.06.2026")],
        [P("TIME TABLE (2026-27 ODD)", stb)] + [""] * 10 + [P("Wef: 01.07.2026")],
        [P(f"Yr/Sem/Sec: {year} YR / {SEM[year]} SEM / {sec}")] + [""] * 5 + [P(f"Class Advisor: {advisor}")] + [""] * 4 + [P("HALL NO.: -")],
        [P("Period"), P("1"), P("2"), P("Break"), P("3"), P("4"), P("Lunch"), P("5"), P("6"), P("Break"), P("7"), P("8")],
        [P("Day/Time"), P(PERIODS[0]), P(PERIODS[1]), P("10.40-10.55"), P(PERIODS[2]), P(PERIODS[3]), P("12.35-01.25"),
         P(PERIODS[4]), P(PERIODS[5]), P("02.55-03.10"), P(PERIODS[6]), P(PERIODS[7])],
    ]
    col_of = [1, 2, 4, 5, 7, 8, 10, 11]
    body, spans = [], []
    for d, day in enumerate(DAYS):
        row = [P(day)] + [""] * 11
        g = plan["grid"][d]
        p = 0
        while p < 8:
            q = p
            while q + 1 < 8 and g[q + 1] == g[p] and col_of[q + 1] == col_of[q] + 1:
                q += 1
            row[col_of[p]] = P(cell_text(g[p]))
            r = 6 + d
            if q > p:
                spans.append(("SPAN", (col_of[p], r), (col_of[q], r)))
            p = q + 1
        row[3], row[6], row[9] = "", "", ""
        body.append(row)
    data = head + body
    style = [("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("SPAN", (0, 0), (10, 0)), ("SPAN", (0, 1), (10, 1)), ("SPAN", (0, 2), (10, 2)),
             ("SPAN", (0, 3), (5, 3)), ("SPAN", (6, 3), (10, 3)),
             ("SPAN", (3, 6), (3, 10)), ("SPAN", (6, 6), (6, 10)), ("SPAN", (9, 6), (9, 10)),
             ("BACKGROUND", (0, 4), (-1, 5), colors.HexColor("#eeeeee"))] + spans
    for c, label in ((3, "Break"), (6, "Lunch Break"), (9, "Break")):
        data[6][c] = P(label)
    widths = [20 * mm] + [24 * mm, 24 * mm, 15 * mm, 24 * mm, 24 * mm, 16 * mm, 24 * mm, 24 * mm, 15 * mm, 24 * mm, 24 * mm]
    t1 = Table(data, colWidths=widths)
    t1.setStyle(TableStyle(style))

    rows = [[P("S.No", stb), P("Acronym", stb), P("Name of the Subject", stb), P("Name of the Faculty", stb), P("Periods/week", stb)]]
    n = 1
    for a, name in cur["theory"]:
        rows.append([P(str(n)), P(a), P(name), P(kiot_person(rng)), P("4")]); n += 1
    for a, name, sw in cur["labs"]:
        rows.append([P(str(n)), P(a), P(name + " (Theory cum Practical)"), P(kiot_person(rng)), P("5")]); n += 1
    rows += [[P(str(n)), P("AS"), P("Aptitude Skills"), P(kiot_person(rng)), P("1")],
             [P(str(n + 1)), P("CS-I"), P("Coding Skills-I"), P(kiot_person(rng)), P("1")],
             [P(str(n + 2)), P("LIB"), P("Library Hour"), P(advisor + f", AP/{dept}"), P("1")],
             [P(str(n + 3)), P("HOD/MENTOR"), P("HOD Hour/Mentor Hour"), P("All Faculty"), P("1")]]
    t2 = Table(rows, colWidths=[14 * mm, 26 * mm, 95 * mm, 90 * mm, 22 * mm])
    t2.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
    sig = Table([[P("TIME TABLE INCHARGE"), P("HOD"), P("DIR-ACADEMICS"), P("PRINCIPAL")]], colWidths=[62 * mm] * 4)
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=8 * mm, bottomMargin=8 * mm)
    doc.build([t1, Spacer(1, 4 * mm), t2, Spacer(1, 10 * mm), sig])


# --------------------------------------------------------------------------
# Events, circulars, reports
# --------------------------------------------------------------------------

ORGS = ["Nimbus Cloudworks", "Codeverge Labs", "DataNest Analytics", "Stackbridge Technologies", "Kovai Robotics",
        "Quantix Systems", "BlueOrbit Software", "Tamarind AI", "SecureNest Labs", "Vertex Learning Solutions"]
ACADEMIA = ["Anna University, Chennai", "NIT Tiruchirappalli", "PSG College of Technology", "IIT Madras",
            "Coimbatore Institute of Technology", "Bharathiar University"]
TOPICS = {
    "cloud": ["AWS core services", "Cloud cost management", "Serverless architectures", "Containers and Kubernetes"],
    "ai": ["Generative AI for developers", "Prompt engineering", "Machine learning pipelines", "Responsible AI"],
    "sec": ["Ethical hacking fundamentals", "Network security", "Malware analysis basics", "Secure coding"],
    "web": ["Full stack development with MERN", "REST API design", "Frontend performance", "DevOps for web apps"],
    "data": ["Data visualization with Power BI", "SQL for analytics", "Big data with Spark", "Data storytelling"],
    "iot": ["IoT with Arduino", "Embedded C", "Sensor networks", "Edge AI"],
    "career": ["Resume building", "Interview preparation", "Aptitude shortcuts", "Group discussion skills"],
    "mgmt": ["Digital marketing", "Startup funding", "Leadership in organisations", "Business analytics"],
}
TOPIC_SW = {"cloud": ("Docker Desktop", "CC11"), "ai": ("Anaconda", "CC5"), "sec": ("VirtualBox", "CC4"),
            "web": ("Node.js", "CC1"), "data": ("Power BI Desktop", "CC10"), "iot": ("Arduino IDE", "CC7")}

HALL_ALIAS = {
    "A Block SH": ["A Block Seminar Hall", "A-Block Seminar Hall", "Seminar Hall, A Block"],
    "CSE PG SH": ["CSE PG Seminar Hall", "PG Seminar Hall (CSE)"],
    "D Block SH": ["D Block Seminar Hall", "D-Block Seminar Hall"],
    "E Block SH": ["E Block Seminar Hall", "E-Block Seminar Hall", "Seminar Hall, E Block"],
    "MBA SH": ["MBA Seminar Hall", "Seminar Hall, MBA Block"],
}


def venue_text(rng, code):
    if code in HALL_ALIAS:
        return rng.choice(HALL_ALIAS[code])
    if code.startswith("CC"):
        return rng.choice([f"{code} Laboratory", f"{code} Lab", code])
    return rng.choice([f"{code} Lab", f"Centre of Excellence ({code})", code])


class Calendar:
    def __init__(self):
        self.slots = defaultdict(list)  # (venue, date) -> [(start, end)]

    def free(self, venues, days, start, end):
        return all(not (s < end and start < e) for v in venues for d in days for s, e in self.slots[(v, d)])

    def take(self, venues, days, start, end):
        for v in venues:
            for d in days:
                self.slots[(v, d)].append((start, end))


def workdays(start, n, saturdays=True):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 or (saturdays and d.weekday() == 5):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def nth_weekday_on_or_after(d, weekday):
    return d + dt.timedelta(days=(weekday - d.weekday()) % 7)


def build_events(rng, plans):
    cal = Calendar()
    events = []
    # Block 2026-27 odd-semester lab slots from the timetables, and the two real sample circulars
    d = dt.date(2026, 7, 1)
    while d <= dt.date(2026, 11, 30):
        if d.weekday() < 5:
            for plan in plans.values():
                for wd, bl, alloc in plan["placed"]:
                    if wd == d.weekday():
                        p0, p1 = BLOCKS[bl]
                        cal.take([a[2] for a in alloc], [d], PERIOD_24[p0][0], PERIOD_24[p1][1])
        d += dt.timedelta(days=1)
    cal.take(["CSE PG SH"], [dt.date(2026, 8, 8)], "09:30", "16:00")
    cal.take(["CC11"], workdays(dt.date(2026, 9, 7), 7), "09:00", "16:40")

    def add(category, title, org, venues, start_date, n_days, start, end, expected, sig, guest, fmt, audience,
            topic_key=None, resource=None, saturdays=True, shift=True):
        days = workdays(start_date, n_days, saturdays)
        tries = 0
        while not cal.free(venues, days, start, end):
            if not shift or tries > 20:
                return None
            start_date += dt.timedelta(days=1)
            days = workdays(start_date, n_days, saturdays)
            tries += 1
        if days[-1] > TODAY:
            return None
        cal.take(venues, days, start, end)
        topics = rng.sample(TOPICS[topic_key], 3) if topic_key else []
        cap = sum(int(VENUE_CAP[v]) for v in venues)
        actual = max(5, min(cap, int(expected * rng.uniform(0.8, 1.15))))
        ev = {"event_id": f"EV{len(events) + 1:04d}", "category": category, "title": title, "organiser": org,
              "venues": venues, "start_date": days[0], "end_date": days[-1], "days": days, "start": start,
              "end": end, "expected": expected, "actual": actual, "significance": sig, "guest_profile": guest,
              "format": fmt, "audience": audience, "topics": topics, "resource": resource or []}
        events.append(ev)
        return ev

    def ext_person(kind):
        if kind == "industry":
            return [f"{person(rng)}, {rng.choice(['Senior Engineer', 'Solutions Architect', 'Founder & CEO', 'Trainer'])}, {rng.choice(ORGS)}"]
        if kind == "external_academic":
            return [f"{person(rng, 'Dr.')}, Professor, {rng.choice(ACADEMIA)}"]
        if kind == "government":
            return [f"{person(rng, 'Thiru')}, {rng.choice(['Deputy Collector', 'Scientist-E, a national space research centre', 'Joint Director, Technical Education'])}"]
        kiot_person(rng)  # keeps the random sequence of the earlier made-up names
        role = rng.choice(["Principal", "Executive Chairman", "HoD"])
        return ["Dr. P. Rajendran, Prof & HoD/CSE, KIOT" if role == "HoD" else f"The {role}, KIOT"]

    for y in (2023, 2024, 2025, 2026):
        jul = dt.date(y, 7, 1)
        # Executive Chairman address to final years (first week of July)
        add("address", "Executive Chairman's Address to Final Year Students", "Principal Office", ["E Block SH"],
            nth_weekday_on_or_after(dt.date(y, 7, 3), 0), 1, "10:00", "12:00", 320, 3, "internal", "talk",
            "IV Year students, all departments", resource=["Executive Chairman, KIOT"])
        # Principal address to II years
        add("address", "Principal's Address to Second Year Students", "Principal Office", ["D Block SH"],
            nth_weekday_on_or_after(dt.date(y, 7, 8), 2), 1, "10:00", "11:30", 280, 2, "internal", "talk",
            "II Year students, all departments", resource=["Principal, KIOT"])
        # HOD address per dept
        for dept, hall, n in (("CSE", "CSE PG SH", 65), ("MBA", "MBA SH", 120), ("ECE", "A Block SH", 90)):
            add("address", f"HoD's Address to {dept} Students - Odd Semester {y}-{(y + 1) % 100:02d}", dept, [hall],
                nth_weekday_on_or_after(dt.date(y, 7, 10), rng.randint(0, 4)), 1, "14:10", "15:55", n, 1,
                "internal", "talk", f"{dept} students", resource=[f"HoD/{dept}, KIOT"])
        # First-year induction / inauguration (3rd week of August) - planted pattern
        add("inauguration", f"Inauguration of First Year Induction Programme {y}-{(y + 1) % 100:02d}", "Principal Office",
            ["E Block SH"], nth_weekday_on_or_after(dt.date(y, 8, 17), 0), 1, "09:30", "13:00", 340, 3,
            "government", "talk", "First Year students and parents", resource=ext_person("government"))
        # In-house hackathon in September (COE labs)
        add("hackathon", f"KIOT In-house Hackathon {y}", "CSE", ["COE1", "COE2", "COE3"],
            nth_weekday_on_or_after(dt.date(y, 9, 12), 4), 2, "09:00", "17:00", 58, 2, "industry", "hands-on",
            "II & III Year CSE+ students (teams of 4)", topic_key=rng.choice(["ai", "web", "iot"]),
            resource=ext_person("industry"))
        # Model practical exams in labs, November weeks 45-47 - planted pattern
        if y < 2026:
            start = nth_weekday_on_or_after(dt.date(y, 11, 6), 0)
            for i, lab in enumerate(["CC1", "CC2", "CC3", "CC4", "CC5", "CC6"]):
                add("exam", "Model Practical Examinations - Odd Semester", "Office of the Controller of Examinations",
                    [lab], start + dt.timedelta(days=7 * (i % 2)), 5, "09:00", "16:40", 30 if lab not in ("CC1", "CC2") else 60,
                    2, "none", "hands-on", "II, III & IV Year students", saturdays=False)
        # FDP in D Block SH, December (planted) - y < 2026
        if y < 2026:
            tk = rng.choice(["ai", "cloud", "sec", "data"])
            add("fdp", f"Five-Day AICTE-Sponsored Faculty Development Programme on {TOPICS[tk][0]}", "CSE", ["D Block SH"],
                nth_weekday_on_or_after(dt.date(y, 12, 8), 0), 5, "09:30", "16:30", 140, 2, "external_academic",
                "mixed", "Faculty members from engineering colleges", topic_key=tk, resource=ext_person("external_academic") + ext_person("industry"),
                saturdays=False)
        # Even-semester (calendar year y+1) items, only if in the past
        y1 = y + 1
        # Placement training Jan-Mar, COE labs, Saturdays - planted pattern
        for m in (1, 2, 3):
            add("placement_training", f"Placement Training Programme - Phase {m} ({y}-{y1 % 100:02d} batch)", "Placement & Training Cell",
                ["COE1", "COE2", "COE3"], nth_weekday_on_or_after(dt.date(y1, m, 6), 5), 1, "09:00", "16:00", 58, 1,
                "industry", "hands-on", "Final Year students", topic_key="career", resource=ext_person("industry"))
        # Graduation day in February, E Block SH
        add("ceremony", f"Graduation Day {y1}", "Principal Office", ["E Block SH"],
            nth_weekday_on_or_after(dt.date(y1, 2, 15), 5), 1, "10:00", "13:00", 345, 3, "government", "talk",
            "Graduating students and parents", resource=ext_person("government"))
        # Admissions counselling in A Block SH, March weeks 10-12 - planted pattern
        add("admissions", f"Admission Counselling and Spot Registration {y1}-{(y1 + 1) % 100:02d}", "Admissions Office",
            ["A Block SH"], nth_weekday_on_or_after(dt.date(y1, 3, 4), 0), 12, "09:00", "17:00", 80, 2, "none",
            "talk", "Prospective students and parents", resource=["Admissions Committee, KIOT"])
        # Summer FDP in June, CSE PG SH
        tk = rng.choice(["ai", "web", "data"])
        add("fdp", f"Three-Day Faculty Development Programme on {TOPICS[tk][1]}", "CSE", ["CSE PG SH"],
            nth_weekday_on_or_after(dt.date(y1, 6, 9), 0), 3, "09:30", "16:30", 55, 1, "industry", "mixed",
            "Faculty members", topic_key=tk, resource=ext_person("industry"), saturdays=False)

        # Guest lectures, workshops, value-added courses spread through the year
        months = [7, 8, 9, 10, 11, 12, 1, 2, 3, 4]
        for _ in range(12):
            m = rng.choice(months)
            yy = y if m >= 7 else y1
            d0 = dt.date(yy, m, rng.randint(1, 26))
            dept = rng.choice(["CSE", "CSE", "IT", "AI&DS", "CSBS", "ECE", "MBA"])
            tk = rng.choice(["mgmt", "career"]) if dept == "MBA" else rng.choice(["cloud", "ai", "sec", "web", "data", "iot"])
            kind = rng.choice(["industry", "external_academic"])
            n = rng.choice([45, 60, 65, 90, 120, 140])
            if dept == "MBA":
                hall = "MBA SH"
            elif n <= 70:
                hall = "CSE PG SH" if dept in ("CSE", "AI&DS", "CSBS") else "A Block SH"
            elif n <= 100:
                hall = "A Block SH"
            else:
                hall = "D Block SH"
            if hall == "A Block SH" and m == 3:
                hall = "D Block SH"
            add("guest_lecture", f"Guest Lecture on {rng.choice(TOPICS[tk])}", dept, [hall], d0, 1,
                rng.choice(["10:00", "10:55", "13:25"]), rng.choice(["12:35", "15:55"]), n, rng.choice([1, 2]), kind,
                "talk", f"{rng.choice(['II', 'III', 'II & III', 'III & IV'])} Year {dept} students", topic_key=tk,
                resource=ext_person(kind))
        for _ in range(6):
            m = rng.choice(months)
            yy = y if m >= 7 else y1
            d0 = nth_weekday_on_or_after(dt.date(yy, m, rng.randint(1, 22)), 5)
            tk = rng.choice(list(TOPIC_SW))
            sw, lab = TOPIC_SW[tk]
            labs = [lab]
            n = 28
            if rng.random() < 0.4:
                labs = ["CC1"] if tk != "web" else ["CC2"]
                labs = labs if sw in LAB_SW[labs[0]] else [lab]
                n = 55 if labs[0] in ("CC1", "CC2") else 28
            add("workshop", f"Hands-on Workshop on {TOPICS[tk][rng.randint(0, 3)]}", rng.choice(["CSE", "IT", "AI&DS"]),
                labs, d0, 1, "09:30", "16:00", n, 1, "industry", "hands-on", "II & III Year students",
                topic_key=tk, resource=ext_person("industry"))
        for _ in range(3):
            m = rng.choice([8, 9, 10, 1, 2])
            yy = y if m >= 7 else y1
            tk = rng.choice(["cloud", "ai", "sec", "data"])
            add("value_added_course", f"Value-Added Course: {TOPICS[tk][0]}", rng.choice(["CSE", "IT", "AI&DS"]),
                [TOPIC_SW[tk][1]], dt.date(yy, m, rng.randint(1, 15)), 7, "15:10", "16:40", 28, 1, "industry",
                "hands-on", "Registered students", topic_key=tk, resource=ext_person("industry"))
        for _ in range(4):
            m = rng.choice(months)
            yy = y if m >= 7 else y1
            add("contest", f"CodeSprint - In-house Coding Contest", "CSE", [rng.choice(["COE1", "COE2", "COE3"])],
                nth_weekday_on_or_after(dt.date(yy, m, rng.randint(1, 22)), 5), 1, "10:00", "13:00", 20, 1, "none",
                "hands-on", "II & III Year students (BYOD)")
        # Small but significant: VIP interaction with a small audience in a big hall
        m = rng.choice([9, 10, 1, 2])
        yy = y if m >= 7 else y1
        add("guest_lecture", "Special Interaction Session with a Space Scientist", "IQAC", [rng.choice(["D Block SH", "E Block SH"])],
            dt.date(yy, m, rng.randint(2, 24)), 1, "11:00", "12:30", rng.choice([40, 45, 50]), 3, "government", "talk",
            "Selected students and faculty", topic_key="ai", resource=ext_person("government"))
    events.sort(key=lambda e: (e["start_date"], e["start"]))
    return events


VENUE_CAP = {}


def fmt_date(rng, d):
    return d.strftime("%d.%m.%Y") if rng.random() < 0.75 else d.strftime("%d %B %Y").lstrip("0")


def fmt_time(rng, t):
    h, m = map(int, t.split(":"))
    if rng.random() < 0.6:
        ap = "AM" if h < 12 else "PM"
        hh = h if h <= 12 else h - 12
        return f"{hh:02d}.{m:02d} {ap}"
    return t


def ay_of(d):
    return f"{d.year}-{(d.year + 1) % 100:02d}" if d.month >= 7 else f"{d.year - 1}-{d.year % 100:02d}"


ISSUER_CODE = {"Principal Office": "PRIN", "Office of the Controller of Examinations": "COE-EXAM",
               "Placement & Training Cell": "PAT", "Admissions Office": "ADM", "IQAC": "IQAC"}


def styles():
    base = ParagraphStyle("b", fontName="Times-Roman", fontSize=11, leading=15, alignment=TA_JUSTIFY)
    return {
        "b": base,
        "c": ParagraphStyle("c", parent=base, alignment=TA_CENTER),
        "h": ParagraphStyle("h", parent=base, fontName="Times-Bold", fontSize=14, leading=17, alignment=TA_CENTER),
        "hb": ParagraphStyle("hb", parent=base, fontName="Times-Bold", alignment=TA_CENTER),
        "s": ParagraphStyle("s", parent=base, fontSize=9, leading=11, alignment=TA_CENTER),
    }


def letterhead(S, circ_no, date_txt):
    P = Paragraph
    t = Table([
        [P("<b>KIOT</b>", S["c"]), P(f"<b>{COLLEGE}</b><br/>(An Autonomous Institution)", S["h"]), ""],
        ["", P("Approved by AICTE, Affiliated to Anna University,<br/>Accredited by NAAC 'A' Grade and NBA (B.E.: Mech, ECE, EEE &amp; CSE)", S["c"]), ""],
        ["", P("Kakapalayam (PO), Salem - 637 504 &nbsp;&nbsp;&nbsp; www.kiot.ac.in", S["c"]), ""],
        [P("<b>IQAC</b>", S["c"]), P(circ_no, S["b"]), P(date_txt, S["c"])],
    ], colWidths=[28 * mm, 122 * mm, 30 * mm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("SPAN", (0, 0), (0, 2)),
                           ("SPAN", (2, 0), (2, 2)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    return t


def distribution(S, depts):
    names = ["MECH", "ECE", "EEE", "CSE", "CIVIL", "IT", "CSBS", "AI&DS", "MBA", "MCA", "S&H", "IQAC", "LIB.", "COE", "PAT"]
    t = Table([[Paragraph(n.replace("&", "&amp;"), S["s"]) for n in names],
               [Paragraph("*" if n in depts else "", S["s"]) for n in names]], colWidths=[12 * mm] * len(names))
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    return t


def render_circular(path, ev, circ_no, rng, S):
    P = Paragraph
    issue = ev["start_date"] - dt.timedelta(days=rng.randint(1, 6))
    issued_by = {"CSE": "Department of CSE", "IT": "Department of IT", "AI&DS": "Department of AI&amp;DS",
                 "CSBS": "Department of CSBS", "ECE": "Department of ECE", "MBA": "Department of MBA"}.get(
        ev["organiser"], ev["organiser"].replace("&", "&amp;"))
    when = fmt_date(rng, ev["start_date"]) if ev["start_date"] == ev["end_date"] else \
        f"{fmt_date(rng, ev['start_date'])}<br/>to<br/>{fmt_date(rng, ev['end_date'])}"
    when += f"<br/>{fmt_time(rng, ev['start'])} to {fmt_time(rng, ev['end'])}"
    vtxt = "<br/>".join(venue_text(rng, v) for v in ev["venues"])
    story = [letterhead(S, f"Circular No. {circ_no}", issue.strftime("%d.%m.%Y")),
             Table([[P("<b>CIRCULAR</b>", S["hb"])]], colWidths=[180 * mm], style=[("BOX", (0, 0), (-1, -1), 0.6, colors.black)]),
             Table([[P("To", S["b"]), P(ev["audience"].replace("&", "&amp;"), S["b"])],
                    [P("Subject", S["b"]), P(f"{ev['title'].replace('&', '&amp;')} - Reg.", S["b"])],
                    [P("Circular issued by", S["b"]), P(issued_by, S["b"])]], colWidths=[45 * mm, 135 * mm],
                   style=[("GRID", (0, 0), (-1, -1), 0.6, colors.black)]),
             Spacer(1, 5 * mm)]
    lead = {
        "admissions": "The Admissions Office will conduct admission counselling and spot registration for the forthcoming academic year. Prospective students and parents may visit the counselling desk with original certificates.",
        "exam": "The model practical examinations for the odd semester are scheduled as below. Students must report to the respective laboratories 15 minutes before the session with their record notebooks.",
        "placement_training": "The Placement &amp; Training Cell is conducting a placement training programme for final year students. Students must bring their own laptops (BYOD) with the required software installed.",
        "contest": "An in-house coding contest is being organised to strengthen problem-solving skills. Participants must bring their own laptops (BYOD).",
    }.get(ev["category"], f"We are glad to inform you that the {issued_by} is organizing <b>{ev['title'].replace('&', '&amp;')}</b> for {ev['audience'].replace('&', '&amp;')}. "
                          + ("The session will cover " + ", ".join(ev["topics"]) + "." if ev["topics"] else ""))
    story.append(P(lead, S["b"]))
    story.append(Spacer(1, 4 * mm))
    rp = "<br/>".join(r.replace("&", "&amp;") for r in ev["resource"]) or "-"
    story.append(Table([[P("<b>Name of the Programme</b>", S["c"]), P("<b>Venue</b>", S["c"]), P("<b>Date</b>", S["c"]), P("<b>Resource Persons</b>", S["c"])],
                        [P(ev["title"].replace("&", "&amp;"), S["c"]), P(vtxt, S["c"]), P(when, S["c"]), P(rp, S["c"])]],
                       colWidths=[45 * mm, 35 * mm, 40 * mm, 60 * mm],
                       style=[("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8"))]))
    story += [Spacer(1, 4 * mm), P("<u><i><b>Target Audience:</b></i></u>", S["b"]),
              P(f"&gt; {ev['audience'].replace('&', '&amp;')}" + (f" (expected about {ev['expected']} participants)." if rng.random() < 0.5 else "."), S["b"]),
              Spacer(1, 4 * mm), P("For further details, kindly contact:", S["b"]),
              P(f"1. {kiot_person(rng)}, Coordinator", S["b"]),
              Spacer(1, 12 * mm),
              Table([[P("<b>COORDINATOR</b>", S["c"]), P("<b>HoD</b>", S["c"]), P("<b>PRINCIPAL</b>", S["c"])]],
                    colWidths=[60 * mm] * 3, style=[("GRID", (0, 0), (-1, -1), 0.6, colors.black)]),
              Spacer(1, 4 * mm),
              distribution(S, {ev["organiser"]} if ev["organiser"] in DEPTS else set(DEPTS)),
              Spacer(1, 3 * mm),
              P("File: 1) Executive Chairman - File Copy 2) Principal Office 3) Concerned issuing department", S["b"])]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm,
                      bottomMargin=12 * mm).build(story)
    return issue


def render_report(path, ev, rng, S):
    P = Paragraph
    dates = fmt_date(rng, ev["start_date"]) if ev["start_date"] == ev["end_date"] else \
        f"{fmt_date(rng, ev['start_date'])} to {fmt_date(rng, ev['end_date'])} ({len(ev['days'])} days)"
    vtxt = ", ".join(venue_text(rng, v) for v in ev["venues"])
    rows = [("Name of the Event", ev["title"]), ("Organised by", ev["organiser"]), ("Date(s)", dates),
            ("Time", f"{fmt_time(rng, ev['start'])} to {fmt_time(rng, ev['end'])}"), ("Venue", vtxt),
            ("Resource Person(s)", "; ".join(ev["resource"]) or "-"), ("Target Audience", ev["audience"]),
            ("No. of Participants", f"{ev['actual']}" + (f" (registered: {ev['expected']})" if rng.random() < 0.5 else ""))]
    t = Table([[P(f"<b>{k}</b>", S["b"]), P(str(v).replace("&", "&amp;"), S["b"])] for k, v in rows],
              colWidths=[50 * mm, 130 * mm], style=[("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
    summary = {
        "admissions": f"The admission counselling desk functioned for {len(ev['days'])} days. On average about {ev['actual']} candidates and parents visited per day; queries on courses, fees and hostel facilities were addressed.",
        "exam": f"The model practical examinations were conducted smoothly with {ev['actual']} students appearing in the laboratory.",
    }.get(ev["category"], f"The event was attended by {ev['actual']} participants. "
          + (f"The resource person(s) covered {', '.join(ev['topics'])}. " if ev["topics"] else "")
          + rng.choice(["Participants found the session highly useful.", "The interactive Q&A session was well received.",
                        "Feedback collected from participants was positive (average rating 4.4/5)."]))
    story = [letterhead(S, f"Report No. {ev['event_id']}/{ay_of(ev['start_date'])}", ev["end_date"].strftime("%d.%m.%Y")),
             Table([[P("<b>EVENT REPORT</b>", S["hb"])]], colWidths=[180 * mm], style=[("BOX", (0, 0), (-1, -1), 0.6, colors.black)]),
             Spacer(1, 4 * mm), t, Spacer(1, 5 * mm), P("<b>Summary</b>", S["b"]), P(summary.replace("&", "&amp;"), S["b"])]
    if ev["topics"]:
        story += [Spacer(1, 3 * mm), P("<b>Topics covered</b>", S["b"])] + [P(f"&bull; {x}", S["b"]) for x in ev["topics"]]
    story += [Spacer(1, 3 * mm), P("<b>Outcome</b>", S["b"]),
              P(rng.choice(["Students gained practical exposure to current industry practices.",
                            "Participants were motivated to pursue certifications in the area.",
                            "The programme met its stated objectives."]), S["b"]),
              Spacer(1, 15 * mm),
              Table([[P("<b>COORDINATOR</b>", S["c"]), P("<b>HoD</b>", S["c"]), P("<b>PRINCIPAL</b>", S["c"])]],
                    colWidths=[60 * mm] * 3, style=[("GRID", (0, 0), (-1, -1), 0.6, colors.black)])]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm,
                      bottomMargin=12 * mm).build(story)


def slug(s, n=60):
    return "".join(c if c.isalnum() else "_" for c in s.upper()).strip("_")[:n].rstrip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/mock")
    args = ap.parse_args()
    out = Path(args.out)
    rng = random.Random(SEED)
    for sub in ("timetables", "drive/circulars", "drive/reports", "ground_truth"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    venues = build_venues()
    VENUE_CAP.update({v["code"]: v["capacity"] for v in venues})
    with open(out / "venues.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(venues[0]))
        w.writeheader(); w.writerows(venues)
    fac = build_faculty(rng)
    with open(out / "faculty.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fac[0]))
        w.writeheader(); w.writerows(fac)

    plans = schedule_timetables(rng, venues)
    truth = []
    for cls, plan in plans.items():
        dept, year, sec = cls
        name = f"TT_{dept.replace('&', '')}_{year}_YR_{sec}.pdf"
        render_timetable(out / "timetables" / name, cls, plan, rng)
        for d, bl, alloc in plan["placed"]:
            p0, p1 = BLOCKS[bl]
            for batch, subj, lab in alloc:
                truth.append({"file": name, "dept": dept, "year": year, "section": sec, "weekday": DAYS[d],
                              "start": PERIOD_24[p0][0], "end": PERIOD_24[p1][1], "subject": subj[0],
                              "subject_name": subj[1], "batch": batch, "venue": lab,
                              "valid_from": "2026-07-01", "valid_to": "2026-11-30"})
    with open(out / "timetables" / "lab_slots_truth.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(truth[0]))
        w.writeheader(); w.writerows(truth)

    events = build_events(rng, plans)
    S = styles()
    counters = defaultdict(int)
    gt = []
    for ev in events:
        code = ISSUER_CODE.get(ev["organiser"], ev["organiser"].replace("&", ""))
        ay = ay_of(ev["start_date"])
        counters[(code, ay)] += 1
        num = counters[(code, ay)]
        if code == "CSE" and ay == "2026-27" and num in (157, 238):
            counters[(code, ay)] += 1; num += 1
        circ_no = f"KIOT/{code}/{ay}/{num:03d}"
        fname = f"CIRCULAR NO.{num:03d} ({code} {ay}) - {slug(ev['title'])}.pdf"
        issue = render_circular(out / "drive" / "circulars" / fname, ev, circ_no, rng, S)
        has_report = ev["category"] != "contest" and rng.random() < 0.8
        rname = ""
        if has_report:
            rname = f"REPORT {ev['event_id']} - {slug(ev['title'])}.pdf"
            render_report(out / "drive" / "reports" / rname, ev, rng, S)
        gt.append({"event_id": ev["event_id"], "circular_no": circ_no, "circular_file": fname, "report_file": rname,
                   "issued_on": issue.isoformat(), "category": ev["category"], "title": ev["title"],
                   "organiser": ev["organiser"], "venues": ";".join(ev["venues"]),
                   "start_date": ev["start_date"].isoformat(), "end_date": ev["end_date"].isoformat(),
                   "n_days": len(ev["days"]), "start_time": ev["start"], "end_time": ev["end"],
                   "expected_headcount": ev["expected"], "actual_headcount": ev["actual"],
                   "significance": ev["significance"], "guest_profile": ev["guest_profile"], "format": ev["format"],
                   "audience": ev["audience"], "topics": ";".join(ev["topics"]),
                   "resource_persons": " | ".join(ev["resource"])})
    with open(out / "ground_truth" / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(gt[0]))
        w.writeheader(); w.writerows(gt)
    print(json.dumps({"venues": len(venues), "faculty": len(fac), "timetables": len(plans),
                      "lab_slots": len(truth), "events": len(events),
                      "reports": sum(1 for g in gt if g["report_file"])}, indent=1))


if __name__ == "__main__":
    main()
