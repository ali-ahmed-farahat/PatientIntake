# Patient Intake Questionnaire System

A bilingual (English / Arabic) medical intake questionnaire and patient assessment platform built using:

- HTML5
- CSS3
- JavaScript
- Python Flask
- SQLite

The system digitizes a comprehensive patient intake process, allowing patients to complete medical questionnaires online while providing healthcare professionals with structured, searchable, and analyzable medical data.

---

# Overview

Traditional patient intake forms are often paper-based or stored as PDFs, making them difficult to manage, search, analyze, and integrate into clinical workflows.

This project transforms a multi-page medical questionnaire into an interactive web application that:

- Collects patient information digitally
- Stores responses in a database
- Supports English and Arabic languages
- Organizes medical history in a structured format
- Supports document and image uploads
- Provides a foundation for AI-assisted medical workflows

---

# Features

## Bilingual User Interface

The questionnaire is fully bilingual:

- English
- Arabic

All major sections, labels, medical terms, and disease descriptions are provided in both languages.

---

## Personal Information

Collects:

- Patient Code
- Full Name
- Age
- Nationality
- Occupation
- Mobile Number
- Email Address

---

## Marital Status & Fertility History

Supports:

- Single
- Married
- Divorced
- Widow

Additional information:

- Duration of Marriage
- Number of Wives
- Number of Children
- Youngest Child Age
- Fertility Intentions
- Previous Attempts to Conceive
- Contraception Methods
- Divorce Related to Medical Complaint

---

## Allergies

Records:

- Drug Allergies
- Other Allergies

---

## Referral Tracking

Tracks how patients discovered the clinic:

- Google
- Facebook
- Instagram
- TikTok
- Referral

---

## Chronic Diseases & Medical History

The system contains a comprehensive disease assessment section.

### Covered Systems

1. Heart & Vascular
2. Metabolic & Endocrine
3. Neurological
4. Psychiatric
5. Liver & Kidney
6. Sleeping & Breathing
7. Immunity & Inflammatory
8. Oncology & Cancer Treatment
9. Surgical History

For each condition the patient can provide:

- Disease Name
- Duration
- Control Status
- Medication & Dosage
- Sexual Relevance

---

## Medical Education Layer

Patients are provided with simplified explanations for common medical conditions.

### Example

**Hypertension**

High blood pressure

**Diabetes**

High blood sugar caused by impaired insulin function

This improves patient understanding and data accuracy.

---

## Lifestyle & Habits Assessment

### Sleep

- Sleep Hours
- Sleep Quality
- Sleep Type
- Sleep Apnea Symptoms
- Daytime Sleepiness

### Physical Activity

- Exercise Frequency
- Exercise Type
- Sitting Hours

### Body Metrics

- Weight
- Height
- BMI
- Waist Circumference

### Substance Use

- Smoking Status
- Cigarettes Per Day
- Alcohol Consumption
- Recreational Drug Use

### Sexual Behavior Factors

- Pornography Usage
- Masturbation
- Partner-Specific Difficulties

---

## Psychological & Occupational Assessment

Records:

- Stress Level
- Anxiety
- Depression
- Relationship Conflict
- Performance Anxiety
- Sedentary Work
- Night Shift Work
- Heat Exposure
- Toxin Exposure

Recovery indicators:

- Energy Level
- Libido Score
- Recovery Score

---

## Clinical Complaint Classification

Supports multiple clinical pathways:

### Sexual Health

- Erectile Dysfunction
- Premature Ejaculation
- Delayed Ejaculation
- Low Libido

### Fertility

- Male Infertility
- Delayed Conception

### Hormonal

- Low Testosterone Symptoms

### Structural Conditions

- Peyronie's Disease
- Penile Curvature
- Penile Size Concerns

### Pain Conditions

- Pelvic Pain
- Genital Pain
- Testicular Pain

### Infection

- Sexually Transmitted Infections

### Follow-up

- Routine Follow-Up Visits

---

## Physician Assessment Notes

Dedicated section for healthcare providers to document:

- Initial Assessment
- Observations
- Treatment Considerations
- Follow-Up Notes

---

# File Upload System

The platform supports medical document uploads.

## Drug Image Upload

Patients can upload:

- Medication Boxes
- Prescription Labels
- Drug Packaging
- Medication Bottles

Purpose:

- Medication Identification
- Active Ingredient Detection
- AI-Assisted Drug Recognition

---

## Current Medications

Patients can manually enter:

- Medication Name
- Dosage
- Frequency
- Duration

---

## Medical History Documents

Supported file types:

- PDF
- JPG
- JPEG
- PNG

Examples:

- Referral Letters
- Previous Medical Reports
- Hospital Discharge Summaries
- Consultation Notes

---

## Investigation & Laboratory Results

Patients can upload:

### Blood Tests

- CBC
- Lipid Profile
- HbA1c
- Liver Function
- Kidney Function

### Hormonal Panels

- Testosterone
- Free Testosterone
- LH
- FSH
- Prolactin
- Estradiol
- SHBG

### Fertility Tests

- Semen Analysis

### Imaging

- Ultrasound
- MRI
- CT Scan

---

# AI Medication Recognition (Roadmap)

Future versions will support AI-powered medication analysis.

## OCR Processing

The system will:

1. Scan uploaded medication images
2. Extract visible text
3. Identify medication names

Possible technologies:

- Tesseract OCR
- EasyOCR

---

## Drug Database Lookup

Planned integrations:

### openFDA

Used for:

- Drug Labels
- Drug Information
- FDA Safety Data

### DrugBank

Used for:

- Drug Interactions
- Active Ingredients
- Clinical Information

---

## AI Clinical Assistance

Future features:

### Medication Analysis

- Duplicate Medication Detection
- Drug Interaction Screening
- Fertility Impact Screening
- Testosterone Impact Analysis

### Investigation Analysis

Automatic review of:

- Hormonal Profiles
- Semen Analysis
- Blood Tests

### Clinical Risk Scoring

Potential support for:

- Cardiovascular Risk
- Metabolic Risk
- Sexual Health Risk

---

# Form Validation

The system prevents incomplete submissions.

Validation includes:

- Required Fields
- Browser Validation
- JavaScript Validation
- Data Integrity Checks

Users cannot submit incomplete questionnaires.

---

# Architecture

```text
Patient
   ↓
HTML Form
   ↓
JavaScript
   ↓
Flask API
   ↓
SQLite Database
```

---

# Technology Stack

## Frontend

- HTML5
- CSS3
- JavaScript

## Backend

- Python
- Flask
- Flask-CORS

## Database

- SQLite

## Future Integrations

- PostgreSQL
- OpenFDA
- DrugBank
- OCR Engines
- AI Services

---

# Project Structure

```text
PatientIntake/
│
├── app.py
├── intake.db
├── requirements.txt
├── README.md
│
├── templates/
│   └── index.html
│
├── static/
│   ├── style.css
│   ├── script.js
│
├── uploads/
│   ├── drug_images/
│   ├── medical_history/
│   └── investigations/
│
└── screenshots/
```

---

# Installation

## Clone Repository

```bash
git clone https://github.com/KhaledFadi/PatientIntake.git

cd PatientIntake
```

## Create Virtual Environment

### Windows

```bash
python -m venv venv

venv\Scripts\activate
```

### Linux / Mac

```bash
python3 -m venv venv

source venv/bin/activate
```

## Install Dependencies

```bash
pip install -r requirements.txt
```

## Run Application

```bash
python app.py
```

Application URL:

```text
http://127.0.0.1:5000
```

---

# Database

Database file:

```text
intake.db
```

Main table:

```text
intake_forms
```

Stores:

- Patient Information
- Medical History
- Lifestyle Data
- Clinical Complaints
- Uploaded File References
- Full JSON Questionnaire Data

---

# Viewing Submitted Forms

Admin page:

```text
http://127.0.0.1:5000/submissions
```

Displays:

- Patient Details
- Questionnaire Responses
- Uploaded Documents

---

# Future Enhancements

## Security

- Authentication
- Authorization
- HTTPS
- Role-Based Access Control

## Clinical Dashboard

- Patient Search
- Timeline View
- Advanced Filtering

## Reporting

- PDF Export
- Printable Reports
- Clinical Summaries

## Notifications

- Email Notifications
- SMS Reminders
- Follow-Up Scheduling

## Infrastructure

- PostgreSQL Migration
- Docker Support
- AWS Deployment
- Azure Deployment

---

Patient Intake Questionnaire System

Built using Python Flask, HTML, CSS, JavaScript, and SQLite.
