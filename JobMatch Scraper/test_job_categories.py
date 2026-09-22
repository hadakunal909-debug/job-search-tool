#!/usr/bin/env python3
"""Job-type regression cases: duties first, title second, employer last.

No network/database. Real posting excerpts were read from the local September 2026
JD cache; focused synthetic counterexamples vary one signal at a time. These tests
assert user-visible decisions, not the classifier's keyword lists or implementation.
Run directly with ``python test_job_categories.py``.
"""
import traceback

from job_categories import classify_job


CONSTRUCTION_DUTIES = """Responsibilities
Lead commercial building construction from preconstruction through final turnover.
Manage subcontractors, review RFIs and submittals, and approve change orders.
Coordinate site inspections, building permits, construction schedules and jobsite safety.
"""

IT_DUTIES = """Responsibilities
Lead software implementation and cloud migration projects across enterprise applications.
Coordinate software development, integration testing, API integrations, and production releases.
Manage the SDLC, cybersecurity reviews, and deployment of enterprise IT systems.
"""


def expect(category, title, jd="", company="", company_type="", source=None):
    result = classify_job(title, jd, company, company_type)
    assert result["category"] == category, (title, category, result)
    if source is not None:
        assert result["category_source"] == source, (title, source, result)
    return result


def test_actual_duties_override_both_title_and_company_toward_construction():
    expect("construction", "IT Project Manager", CONSTRUCTION_DUTIES,
           "Example Software", "Software & Internet", source="jd")


def test_actual_duties_override_both_title_and_company_toward_it():
    expect("it", "Construction Project Manager", IT_DUTIES,
           "Example Builders", "Engineering, Construction & Real Estate", source="jd")


def test_it_project_manager_at_construction_company_is_it():
    # The user's explicit precedence example; an employer is not a job description.
    expect("it", "IT Project Manager", IT_DUTIES,
           "Example Builders", "Engineering, Construction & Real Estate", source="jd")


def test_specific_title_beats_company_when_description_has_no_domain():
    generic = "Manage schedules and budgets. Coordinate stakeholders and communicate status."
    expect("it", "IT Project Manager", generic,
           "Example Builders", "Engineering, Construction & Real Estate", source="title")
    expect("construction", "Construction Project Manager", generic,
           "Example Software", "Software & Internet", source="title")


def test_one_unambiguous_duty_still_overrides_conflicting_title():
    expect("construction", "IT Project Manager", "Lead commercial building construction projects.", source="jd")
    expect("it", "Construction Project Manager", "Lead enterprise software implementation.", source="jd")


def test_flattened_sections_still_separate_employer_and_qualifications_from_duties():
    jd = ("About us: We are a healthcare company delivering medical devices and clinical research. "
          "Responsibilities: Lead software implementation and cloud migration projects. "
          "Manage software development and cybersecurity reviews. "
          "Required Qualifications: Experience in medical devices, clinical trials and patient care.")
    expect("it", "Project Manager", jd, "Example Health", "Healthcare, Pharma & Biotech", source="jd")


def test_data_center_construction_is_not_data_analytics():
    expect("construction", "Data Center Construction Project Manager", source="title")


def test_named_occupation_beats_an_incidental_domain_in_a_compound_title():
    # Real cached title shapes that previously fell through to an unrelated company sector.
    expect("it", "AI Senior Software Engineer", "", "Example Bank",
           "Banking, Finance & Insurance", source="title")
    expect("it", "Software Engineer - Technology & Medical Organizations", "", "Example Health",
           "Healthcare, Pharma & Biotech", source="title")
    expect("data", "Sr. Data Analyst, Finance Analytics", "", "Example Software",
           "Software & Internet", source="title")
    expect("data", "Sales Business Data Analyst", source="title")


def test_real_construction_coordinator_procurement_is_in_construction_context():
    # Actalent's Detroit electrical-construction posting includes purchasing duties;
    # that does not turn administration of a construction project into warehouse work.
    jd = """Job Description: Electrical construction contractor focusing on commercial projects
Responsibilities Provide administrative and coordination support to Project Managers.
Assist with processing purchase orders, invoices, change orders, and project paperwork.
Support material procurement activities and monitor delivery status.
Essential Skills Minimum of 2 years of project coordination experience.
"""
    expect("construction", "Construction Project Coordinator", jd, "Actalent", source="jd")


def test_embedded_software_duties_are_not_the_electrical_engineering_degree():
    # Actalent's flat descriptions put "Essential Skills" on the same line as duties.
    jd = ("Responsibilities Lead development of Linux kernel components and device drivers. "
          "Write and maintain source code in C++ for embedded Linux applications. "
          "Perform software testing and support software development. "
          "Essential Skills Bachelor's degree in Electrical Engineering. "
          "Experience in mechanical systems and manufacturing processes.")
    expect("it", "Embedded Software Engineer", jd, source="jd")


def test_company_type_is_an_explicit_lower_confidence_fallback():
    result = expect("construction", "Project Manager", "",
                    "Example Company", "Engineering, Construction & Real Estate", source="company")
    assert result["category_confidence"] != "high", result
    expect("it", "Project Manager", "", "Example Company", "Software & Internet", source="company")


def test_no_evidence_remains_visibly_unclear():
    for title in ("", "Project Manager", "Coordinator II"):
        result = expect("other", title, source="unknown")
        assert result["category_confidence"] == "low", result


def test_careers_shell_cannot_supply_duties_for_a_category():
    shell = "Sorry to interrupt. CSS Error Refresh. " * 30 + IT_DUTIES
    expect("construction", "Construction Project Manager", shell, source="title")
    expect("other", "Project Manager", shell, source="unknown")


def test_unknown_company_type_is_not_invented_into_a_domain():
    expect("other", "Project Manager", "", "Acme", "Unsorted", source="unknown")


def test_real_analog_devices_facilities_pm_beats_semiconductor_background():
    # analogdevices.wd1.myworkdayjobs.com/.../Facilities-Project-Manager_R265421
    jd = """About Analog Devices
Analog Devices is a global semiconductor leader. ADI combines analog, digital, AI,
and software technologies with automation and robotics, mobility, healthcare,
energy and data centers.
Key Responsibilities
Directly lead and manage construction projects across ADI's Camas semiconductor
manufacturing site including offices, R&D labs, and cleanroom facilities.
Engage and coordinate external design firms, contractors, and consultants.
Develop RFPs, evaluate bids, and negotiate contracts with design/build firms and
general contractors; administer master construction agreements and project addenda.
Coordinate permitting activities and interface with governmental agencies.
Review and approve change orders, invoices, and progress payments.
Manage punch-list completion, as-built documentation, warranty collection, and final acceptance.
"""
    expect("construction", "Facilities Project Manager", jd,
           "Analogdevices", "Semiconductors & Hardware", source="jd")


def test_real_abbott_embedded_software_is_it_despite_medical_background():
    # abbott.wd5.myworkdayjobs.com/.../Sr-Embedded-Software-Engineer_31152383
    jd = """Abbott is a global healthcare leader. Our businesses include diagnostics,
medical devices, nutritionals and branded generic medicines.
JOB DESCRIPTION: The Opportunity:
The Sr Embedded Software Engineer oversees the design, development, and validation
of software for embedded systems, ensuring compliance with FDA requirements.
What you'll work on
Design and implement software in current programming languages (e.g. C, C++, C#, python).
Develops, maintains, and updates detailed design and interface specifications.
Supports implementation, development, enhancements, and modifications to software source code.
Debugs, troubleshoots, and isolates software problems.
Participate in software development, verification and validation.
Follow approved Design Control procedures for software development in accordance with FDA guidelines.
"""
    expect("it", "Sr Embedded Software Engineer", jd,
           "Abbott", "Healthcare, Pharma & Biotech", source="jd")


def test_real_epic_generic_pm_is_software_implementation_not_patient_care():
    # epic.avature.net/Careers/FolderDetail/Verona-Wisconsin-United-States-Project-Manager/19220
    jd = """Implementing software that saves lives. Join our Project Management team
and drive impactful projects to improve patient care in healthcare organizations.
Travel across the US as part of a team that leads software installations and
ensures the success of newcomers to the Epic community. Use your project management
skills to present to hospital leadership, coordinate end-user training, and provide
comprehensive support as healthcare providers go live with our software.
No software experience required. Manage projects at the most innovative health
systems on the planet. Our community includes major systems like the Mayo Clinic,
Johns Hopkins, Cleveland Clinic, and Kaiser Permanente.
"""
    expect("it", "Project Manager", jd, "Epic Systems", "Healthcare, Pharma & Biotech", source="jd")


def test_real_microsoft_campus_construction_beats_software_company():
    # apply.careers.microsoft.com/careers/job/1970393556939564
    jd = """Drive construction execution in a large-scale greenfield campus environment,
ensuring building construction activities remain aligned with site development,
utility infrastructure, transportation improvements, and campus-wide construction sequencing.
Manage General Contractor and subcontractor performance, holding construction
partners accountable for safety, quality, staffing, productivity and schedule adherence.
Coordinate project activities with substations, electrical distribution systems,
water supply systems, transportation infrastructure, and other utility systems.
Manage budgets, forecasts, change orders, pay applications and Project Expenditure Requests.
"""
    expect("construction", "Construction Project Manager", jd,
           "Microsoft", "Software & Internet", source="jd")


def test_real_microsoft_networking_pm_contractors_do_not_make_it_construction():
    # apply.careers.microsoft.com/careers/job/1970393556868750
    jd = """Drives obligations across the delivery lifecycle including solution
development, delivery planning, cloud consumption and usage, and delivery management.
Supports negotiation and structuring of fixed-fee subcontractor contracts.
Ensures that the mandatory Information Security Risk Assessment is completed
during project initiation and signed off by accredited InfoSec Delivery Compliance Leads.
Gathers customer insights to shape the definition and ongoing execution of networking projects.
Defines and documents the architecture of the business solution and project approach.
Drives technical governance of design, build, and deployment of proof of concepts and pilots.
"""
    expect("it", "Senior Networking Project Manager - CTJ - TS/SCI", jd,
           "Microsoft", "Software & Internet", source="jd")


def test_it_tools_used_for_construction_do_not_redefine_the_job():
    jd = CONSTRUCTION_DUTIES + """Qualifications
Use Microsoft Office, Excel, Power BI, Teams, Jira and project management software.
"""
    expect("construction", "Project Manager", jd, source="jd")


def test_medical_insurance_benefits_are_not_healthcare_duties():
    benefits = """Benefits
Medical, dental and vision insurance. Health care flexible spending accounts.
Mental health support and medical coverage. Life insurance and paid parental leave.
"""
    expect("construction", "Project Manager", CONSTRUCTION_DUTIES + benefits, source="jd")
    expect("it", "Project Manager", IT_DUTIES + benefits, source="jd")


def test_employer_about_text_cannot_outvote_role_duties_by_repetition():
    jd = ("About us\nWe deliver healthcare, clinical research, medical devices and patient care.\n" * 15
          + "\nResponsibilities\n" + IT_DUTIES)
    expect("it", "Project Manager", jd, "Example Health", "Healthcare, Pharma & Biotech", source="jd")


def test_role_introduction_after_company_overview_does_not_need_a_heading():
    # Reproduced against the deployed classifier: it remained in company-skip mode
    # and inferred construction even though this paragraph names clear software work.
    jd = ("About us\nWe are a construction company building commercial offices and managing subcontractors.\n"
          "In this role, you will lead software implementation and cloud migration projects. "
          "You will oversee software development, API integrations and cybersecurity reviews.")
    expect("it", "Project Manager", jd, "Example Builders",
           "Engineering, Construction & Real Estate", source="jd")


def test_hands_on_system_administration_at_builder_is_it():
    # These are actual administrative duties, not a request to be familiar with
    # ordinary office software. "Systems Engineer" alone cannot establish a domain.
    jd = ("Responsibilities\nMaintain Windows servers and Active Directory. "
          "Troubleshoot DNS, DHCP and VPN connectivity. Configure firewalls and routers. "
          "Provision user access and administer the Microsoft 365 tenant.")
    expect("it", "Systems Engineer", jd, "Example Builders",
           "Engineering, Construction & Real Estate", source="jd")


def test_actual_duties_after_old_eight_thousand_character_boundary_are_read():
    preamble = "About the team\n" + ("We work together and value our colleagues. " * 230)
    assert len(preamble) > 8000
    expect("construction", "Project Manager", preamble + "\n" + CONSTRUCTION_DUTIES,
           "Example Software", "Software & Internet", source="jd")


def test_html_and_plain_text_descriptions_retain_the_same_category():
    html = "<h2>Responsibilities</h2><ul><li>Lead commercial building construction.</li>" \
           "<li>Manage subcontractors, RFIs and submittals.</li>" \
           "<li>Coordinate building permits, site inspections and jobsite safety.</li></ul>"
    expect("construction", "Project Manager", html, source="jd")


def test_clinical_research_duties_do_not_become_it_because_software_is_used():
    jd = """Responsibilities
Manage clinical trials and clinical research studies. Coordinate patient recruitment,
clinical study protocols, IRB submissions and regulatory submissions.
Ensure good clinical practice and patient safety. Use Microsoft Excel for reporting.
"""
    expect("healthcare", "Project Manager", jd, "Example Software", "Software & Internet", source="jd")


def test_distinct_domains_have_separate_categories():
    cases = (
        ("data", "Data Analyst", "Responsibilities: Build statistical models and data analytics dashboards. "
         "Perform data analysis using SQL, data visualization and business intelligence reporting."),
        ("engineering", "Mechanical Engineer", "Responsibilities: Design mechanical systems and manufacturing processes. "
         "Perform mechanical engineering, CAD design, product testing and production engineering."),
        ("finance", "Accounting Manager", "Responsibilities: Own financial reporting, accounting and general ledger. "
         "Prepare financial statements, tax returns and audit documentation."),
        ("operations", "Supply Chain Manager", "Responsibilities: Manage supply chain operations, inventory management, "
         "warehouse operations and logistics. Oversee procurement and distribution."),
        ("marketing", "Marketing Manager", "Responsibilities: Lead marketing campaigns, digital marketing, brand strategy "
         "and sales enablement. Own demand generation and customer acquisition."),
        ("education", "Academic Program Coordinator", "Responsibilities: Coordinate curriculum development, student advising, "
         "academic programs and instructional design. Support teaching and student learning."),
    )
    for category, title, jd in cases:
        expect(category, title, jd)


def test_output_explains_the_decision_and_is_deterministic():
    result = expect("construction", "Project Manager", CONSTRUCTION_DUTIES, source="jd")
    required = {"category", "category_label", "category_source", "category_confidence",
                "category_evidence", "category_version"}
    assert required <= result.keys(), result
    assert result["category_label"], result
    assert result["category_version"], result
    assert result["category_confidence"] in {"high", "medium", "low"}, result
    assert isinstance(result["category_evidence"], str) and result["category_evidence"].strip(), result
    assert result == classify_job("Project Manager", CONSTRUCTION_DUTIES), result


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print("%s/%s passed" % (len(tests) - failed, len(tests)))
    raise SystemExit(bool(failed))
