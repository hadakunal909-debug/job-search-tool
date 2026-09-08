#!/usr/bin/env python3
"""Build companies.json — the data behind /companies.

One row per employer we either scrape or know sponsors, carrying its sector, its careers
link, its LinkedIn search, its H-1B volume and its visa/cap-exempt/agency flags.

    python scripts/build_companies.py            # rewrite companies.json
    python scripts/build_companies.py --report   # sector histogram + the Unsorted head
    python scripts/build_companies.py --check    # exit 1 if a PROMINENT company is Unsorted

Run from the app directory: sponsors.txt, careers_us.md and the sponsor JSONs are read
relative to the cwd, exactly as web.py reads them.

POINT IT AT THE LIVE DATABASE, or the file is built from whatever stale copy is lying around:

    DB_REQUIRE=proxy DB_PROXY_SECRET="$(tr -d '\\r\\n' < .db_proxy_secret)" \\
      DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" python scripts/build_companies.py

Without those, db falls back to the credentials in .streamlit/secrets.toml -- left behind by the
retired Streamlit app and pointing at the Supabase this project moved off on 2026-08-15. The
first two builds of this file went that way and nothing failed: 21,980 rows and 167 boards
instead of 27,613 and 115, which silently omitted 591 live employers. DB_REQUIRE=proxy makes
that a crash instead, and the run now prints which database each half came from.

WHY THE UNIVERSE IS WHAT IT IS. SOURCES + sponsors.txt is 1,633 names, but the live corpus
holds ~391 companies outside that union — boards added through /add, plus corpus spellings
that don't normalize onto a SOURCES name. Omitting them would hide employers that have jobs
in the feed right now, which is the most visible bug this page could have. So the universe is
the union of all four sources, and the corpus is one of them.

THE SECTOR MAP IS DELIBERATELY PARTIAL. There is no company->industry dataset in this repo,
and the one offline taxonomy (scraper/classify_everify.py) leaves ~66% unclassified by its own
admission. So sectors resolve in four layers — curated, the two shipped rule helpers, keywords,
then Unsorted — and the winning layer is recorded in `src` so companies_report.csv can be
reviewed. The top 300 companies carry 88% of live postings and 89% of all H-1B filings, so
curating the head and labelling the tail honestly beats guessing at 2,000 names. --check is the
gate that keeps the head curated; the tail may stay Unsorted, and the page says so.

NOT IN core.py OR web.py ON PURPOSE. These regexes would become a fourth twin next to the
filter triplet (web.py::_filter_rows / app.js::matches / core.prefs_match). The app only ever
reads the resolved string out of companies.json.
"""
import collections
import csv
import datetime
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core                                                    # noqa: E402
import db                                                      # noqa: E402
import scraper                                                 # noqa: E402

OUT_JSON = "companies.json"
OUT_CSV = "companies_report.csv"
SNAPSHOT = "jobs_snapshot.json.gz"

# Index into this list is what a row stores; the order is the page's section order.
SECTORS = [
    "Software & Internet",
    "IT Services & Consulting",
    "Semiconductors & Hardware",
    "Banking, Finance & Insurance",
    "Healthcare, Pharma & Biotech",
    "Universities & Research",
    "Hospitals & Health Systems",
    "Aerospace, Defense & Industrial",
    "Energy & Utilities",
    "Engineering, Construction & Real Estate",
    "Retail, Consumer & Hospitality",
    "Transport, Logistics & Automotive",
    "Media, Telecom & Gaming",
    # Added once the tail was actually read: agencies, counties, states and public school
    # districts had nowhere honest to go. Universities & Research is higher ed and research
    # institutes -- a county government and a K-12 district are neither, and filing them there
    # would have made the one sector an F-1 cares most about (cap-exempt) less trustworthy.
    "Government & Public Sector",
]
UNSORTED = "Unsorted"
# --check fails over this share. See the note at the call site for why a ceiling is needed on
# top of the per-company gate.
UNSORTED_CEILING = 5.0

# ---------------------------------------------------------------- careers URLs
# Harvested from scraper/make_careers.py before that file was deleted: it ran its generator at
# MODULE SCOPE, so merely importing it rewrote careers_us.md. These 166 hand-checked links are
# the only thing in it worth keeping, and ~50 of them (Google, Microsoft, Apple) name companies
# with no readable board at all, so nothing else can supply them.
NATIVE = {
    "Samsara": "https://job-boards.greenhouse.io/samsara",
    "Stripe": "https://job-boards.greenhouse.io/stripe",
    "Verkada": "https://job-boards.greenhouse.io/verkada",
    "Brex": "https://job-boards.greenhouse.io/brex",
    "Datadog": "https://job-boards.greenhouse.io/datadog",
    "Instacart": "https://job-boards.greenhouse.io/instacart",
    "SoFi": "https://job-boards.greenhouse.io/sofi",
    "Scale AI": "https://job-boards.greenhouse.io/scaleai",
    "Airbnb": "https://job-boards.greenhouse.io/airbnb",
    "Databricks": "https://job-boards.greenhouse.io/databricks",
    "Twilio": "https://job-boards.greenhouse.io/twilio",
    "Robinhood": "https://job-boards.greenhouse.io/robinhood",
    "Toast": "https://job-boards.greenhouse.io/toast",
    "Checkr": "https://job-boards.greenhouse.io/checkr",
    "Affirm": "https://job-boards.greenhouse.io/affirm",
    "Flexport": "https://job-boards.greenhouse.io/flexport",
    "MongoDB": "https://job-boards.greenhouse.io/mongodb",
    "Okta": "https://job-boards.greenhouse.io/okta",
    "Palantir": "https://jobs.lever.co/palantir",
    "Ramp": "https://jobs.ashbyhq.com/ramp",
    "Notion": "https://jobs.ashbyhq.com/notion",
    "Vanta": "https://jobs.ashbyhq.com/vanta",
    "Replit": "https://jobs.ashbyhq.com/replit",
    "Cursor": "https://jobs.ashbyhq.com/cursor",
    "Avery Dennison": "https://jobs.smartrecruiters.com/AveryDennison",
    "Experian": "https://jobs.smartrecruiters.com/Experian",
    "Google": "https://careers.google.com/jobs/results/?location=United%20States",
    "Alphabet": "https://careers.google.com/jobs/results/?location=United%20States",
    "Amazon": "https://www.amazon.jobs/en/search?country=USA&loc_query=United+States",
    "Amazon Web Services":
        "https://www.amazon.jobs/en/search?base_query=AWS&country=USA&loc_query=United+States",
    "Microsoft": "https://careers.microsoft.com/v2/global/en/search",
    "Meta Platforms": "https://www.metacareers.com/jobs",
    "Apple": "https://jobs.apple.com/en-us/search?location=united-states-USA",
    "Netflix": "https://explore.jobs.netflix.net/careers",
    "Nvidia": "https://www.nvidia.com/en-us/about-nvidia/careers/",
    "Intel": "https://jobs.intel.com",
    "Oracle": "https://careers.oracle.com/jobs",
    "IBM": "https://www.ibm.com/careers/search",
    "Salesforce": "https://careers.salesforce.com/en/jobs/",
    "Adobe": "https://careers.adobe.com/us/en/search-results",
    "Cisco": "https://jobs.cisco.com/jobs/SearchJobs/",
    "Qualcomm": "https://careers.qualcomm.com/careers",
    "Uber": "https://www.uber.com/us/en/careers/list/",
    "Lyft": "https://www.lyft.com/careers",
    "LinkedIn": "https://careers.linkedin.com/jobs",
    "PayPal": "https://careers.pypl.com/home/",
    "eBay": "https://careers.ebayinc.com/us/en/job-search-results",
    "Pinterest": "https://www.pinterestcareers.com/jobs/",
    "Snap": "https://careers.snap.com/jobs",
    "Block": "https://block.xyz/careers/jobs",
    "DoorDash": "https://careers.doordash.com/",
    "Coinbase": "https://www.coinbase.com/careers/positions",
    "Dropbox": "https://jobs.dropbox.com/all-jobs",
    "Snowflake": "https://careers.snowflake.com/us/en/search-results",
    "Workday": "https://www.workday.com/en-us/company/careers.html",
    "ServiceNow": "https://careers.servicenow.com/jobs/",
    "Atlassian": "https://www.atlassian.com/company/careers/all-jobs",
    "Intuit": "https://www.intuit.com/careers/job-search/",
    "Autodesk": "https://www.autodesk.com/careers/overview",
    "Micron Technology": "https://www.micron.com/careers",
    "Advanced Micro Devices": "https://www.amd.com/en/corporate/careers.html",
    "Tesla": "https://www.tesla.com/careers/search/?country=US",
    "Walmart Global Tech": "https://careers.walmart.com/technology",
    "Comcast": "https://jobs.comcast.com/",
    "Expedia Group": "https://careers.expediagroup.com/jobs/",
    "Booking.com": "https://careers.booking.com/",
    "ByteDance": "https://jobs.bytedance.com/en/position",
    "TikTok": "https://careers.tiktok.com/position",
    "Deloitte": "https://apply.deloitte.com/en_US/careers/SearchJobs",
    "Accenture": "https://www.accenture.com/us-en/careers/jobsearch",
    "Cognizant": "https://careers.cognizant.com/global-en/jobs/",
    "Infosys": "https://career.infosys.com/jobs",
    "Tata Consultancy Services": "https://www.tcs.com/careers/us",
    "Wipro": "https://careers.wipro.com/careers-home/jobs",
    "Capgemini": "https://www.capgemini.com/us-en/careers/jobs/",
    "HCL Technologies": "https://www.hcltech.com/careers",
    "Tech Mahindra": "https://careers.techmahindra.com/",
    "EPAM Systems": "https://www.epam.com/careers/job-listings",
    "Ernst & Young": "https://careers.ey.com/ey/search/",
    "PricewaterhouseCoopers": "https://www.pwc.com/us/en/careers/search-jobs.html",
    "KPMG": "https://www.kpmguscareers.com/jobsearch/",
    "McKinsey & Company": "https://www.mckinsey.com/careers/search-jobs",
    "Boston Consulting Group": "https://careers.bcg.com/global/en/search-results",
    "Booz Allen Hamilton": "https://careers.boozallen.com/jobs",
    "Leidos": "https://careers.leidos.com/search/jobs",
    "JPMorgan Chase": "https://careers.jpmorgan.com/us/en/students/search-results",
    "Goldman Sachs": "https://www.goldmansachs.com/careers/our-firm/students/",
    "Morgan Stanley": "https://www.morganstanley.com/people/students-and-graduates",
    "Citigroup": "https://jobs.citi.com/search-jobs/United%20States/",
    "Bank of America": "https://careers.bankofamerica.com/en-us/job-search",
    "Wells Fargo": "https://www.wellsfargojobs.com/en/jobs/",
    "BlackRock": "https://careers.blackrock.com/students/",
    "Fidelity Investments": "https://jobs.fidelity.com/search-jobs/United%20States/",
    "Bloomberg": "https://careers.bloomberg.com/job/search",
    "Two Sigma": "https://careers.twosigma.com/careers/SearchJobs/",
    "Citadel": "https://www.citadel.com/careers/open-opportunities/",
    "Capital One": "https://www.capitalonecareers.com/search-jobs",
    "Visa": "https://corporate.visa.com/en/jobs/",
    "Mastercard": "https://careers.mastercard.com/us/en/search-results",
    "American Express": "https://aexp.eightfold.ai/careers",
    "Pfizer": "https://www.pfizer.com/about/careers",
    "Johnson & Johnson": "https://www.careers.jnj.com/en/jobs/",
    "Merck": "https://jobs.merck.com/us/en/search-results",
    "Eli Lilly": "https://careers.lilly.com/us/en/search-results",
    "Amgen": "https://careers.amgen.com/en/search-jobs",
    "Genentech": "https://careers.gene.com/us/en/search-results",
    "UnitedHealth Group": "https://careers.unitedhealthgroup.com/search-jobs",
    "CVS Health": "https://jobs.cvshealth.com/us/en/search-results",
    "Dell": "https://jobs.dell.com/",
    "American Airlines": "https://jobs.aa.com/",
    "Bristol-Myers Squibb": "https://careers.bms.com/careers",
    "Texas Instruments": "https://careers.ti.com/",
    "Regeneron Pharmaceuticals": "https://careers.regeneron.com/",
    "GlobalFoundries": "https://careers.gf.com/",
    "Northwell Health": "https://jobs.northwell.edu/",
    "Henry Ford Health": "https://careers.henryford.com/",
    "Blackstone": "https://www.blackstone.com/careers/",
    "MassMutual": "https://careers.massmutual.com/",
    "Edwards Lifesciences": "https://jobs.edwards.com/",
    "Guidewire": "https://careers.guidewire.com/",
    "Jones Lang LaSalle": "https://www.jll.com/en-us/careers",
    "Rockwell Automation":
        "https://www.rockwellautomation.com/en-us/company/about-us/careers.html",
    "Bridgewater Associates": "https://www.bridgewater.com/working-at-bridgewater",
    "S&P Global": "https://careers.spglobal.com/",
    "CME Group": "https://www.cmegroup.com/careers.html",
    "Marqeta": "https://www.marqeta.com/company/careers",
    "Tradeweb": "https://www.tradeweb.com/about-us/careers/",
    "Viasat": "https://careers.viasat.com/",
    "W.W. Grainger": "https://jobs.grainger.com/",
    "Altair": "https://careers.altair.com/",
    "Santander": "https://www.santandercareers.com/",
    "ChargePoint": "https://www.chargepoint.com/about/careers",
    "BioMarin": "https://careers.biomarin.com/",
    "Ciena": "https://www.ciena.com/about/careers",
    "Anaplan": "https://www.anaplan.com/company/careers/",
    "National Grid": "https://careers.nationalgrid.com/",
    "DigitalOcean": "https://www.digitalocean.com/careers",
    "Frontier Airlines": "https://www.flyfrontier.com/about-us/careers/",
    "Toyota Motor North America": "https://www.toyota.com/careers/",
    "Alcon": "https://www.alcon.com/careers",
    "Q2": "https://www.q2.com/careers",
    "Eightfold AI": "https://eightfold.ai/careers/",
    "Aurora Innovation": "https://aurora.tech/careers",
    "AIG": "https://www.aig.com/careers",
    "Novant Health": "https://careers.novanthealth.org/",
    "WorldQuant": "https://www.worldquant.com/career-listing/",
    "Cigna": "https://jobs.thecignagroup.com/",
    "Celonis": "https://www.celonis.com/careers/jobs/",
    "Tenneco": "https://careers.tenneco.com/",
    "Clarivate": "https://careers.clarivate.com/",
    "May Mobility": "https://maymobility.com/careers/",
    "New Relic": "https://newrelic.com/about/careers",
    "BitGo": "https://www.bitgo.com/careers/",
    "Sift Science": "https://sift.com/careers",
    "Macy's": "https://www.macysjobs.com/",
    "DirecTV": "https://www.directv.com/careers/",
    "RingCentral": "https://www.ringcentral.com/careers.html",
    "LendingClub": "https://www.lendingclub.com/company/careers",
    "Jacobs": "https://careers.jacobs.com/",
    "Slalom": "https://www.slalom.com/careers",
    "Arcadis": "https://www.arcadis.com/en-us/careers",
    "AlixPartners": "https://www.alixpartners.com/careers/",
    "RSM US": "https://rsmus.com/careers.html",
    "Teradata": "https://careers.teradata.com/",
    "Saviynt": "https://saviynt.com/careers",
    "Sumitomo Mitsui Banking Corporation": "https://www.smbcgroup.com/americas/careers",
}

# These three names are ambiguous in a LinkedIn keyword search. Everything else derives from
# the display name on the client, so only the overrides ship.
LI_KEYWORD = {"Visa": "Visa Inc", "Block": "Block Inc", "Snap": "Snap Inc"}

# Board hosts common enough that storing the prefix once beats storing it per row. 4 of these
# cover roughly a third of every board URL in SOURCES.
PREFIX = {
    "gh": "https://job-boards.greenhouse.io/",
    "sr": "https://jobs.smartrecruiters.com/",
    "ab": "https://jobs.ashbyhq.com/",
    "lv": "https://jobs.lever.co/",
}

KIND_NONE, KIND_NATIVE, KIND_BOARD, KIND_SITE = 0, 1, 2, 3

# Flag bits 0-4 ARE core._VISA_BITS, reused rather than restated so they cannot drift from
# core.VISA_TAGS. 5 and 6 are ours.
BIT_CAP_EXEMPT = 32
BIT_AGENCY = 64

# --------------------------------------------------------------- sector layers
# Keyword layer, first match wins, so this runs most-specific to most-general. A term is here
# only when it NAMES AN INDUSTRY: legal suffixes ("technologies", "group", "labs") are banned,
# which is exactly where scraper/classify_everify.py goes wrong -- anything containing "tech"
# lands in IT consulting there.
KEYWORDS = [
    ("Semiconductors & Hardware",
     r"semiconductor|microelectronic|foundry|wafer|lithograph|photonic|optoelectronic"
     r"|\bchip\b|\bfpga\b|\basic\b|\bcpu\b|\bgpu\b"
     r"|micron|nvidia|qualcomm|broadcom|marvell|synopsys|cadence|lam research"
     r"|applied materials|\bkla\b|\basml\b|analog devices|texas instruments|onsemi"
     r"|globalfoundries|skyworks|qorvo|microchip|infineon|renesas|ampere computing"
     r"|western digital|seagate|sandisk|\bamd\b|\btsmc\b|\bviasat\b|\bciena\b"),
    ("Universities & Research",
     r"universit|college|polytechnic|institute of technology|graduate school"
     r"|school of (?:medicine|public health|nursing|engineering|law|business)"
     r"|research institute|national lab|\bacadem|\bseminary\b"
     r"|\bsmithsonian\b|jet propulsion|brookhaven|fermilab|oak ridge|sandia|los alamos"
     r"|argonne|lawrence livermore|battelle|scripps research|broad institute"
     r"|howard hughes medical|salk institute|\bmitre\b|rand corporation"),
    ("Hospitals & Health Systems",
     r"hospital|health system|healthcare system|medical cent(?:er|re)|\bclinic\b"
     r"|cancer (?:cent(?:er|re)|institute)|children'?s health|\bhealth network\b"
     r"|\bmedical group\b|\bhealth partners\b|\bregional health\b|\bnursing home\b"
     r"|\bhospice\b|\bdialysis\b"
     r"|mayo|cleveland clinic|kaiser permanente|dana.farber|memorial sloan|md anderson"
     r"|mass general|brigham and women|northwell|mount sinai|cedars.sinai"
     r"|houston methodist|city of hope|novant health|henry ford health"),
    ("Healthcare, Pharma & Biotech",
     r"pharmaceutic|\bpharma\b|biotech|biopharma|therapeutic|\bbiosciences?\b"
     r"|life sciences|\bgenomic|\bvaccine|medical device|diagnostic"
     r"|\bhealth\b|\bhealthcare\b|\bmedical\b|\bmedicine\b"
     r"|pfizer|merck|\bamgen\b|genentech|astrazeneca|novartis|\bsanofi\b|\bbayer\b"
     r"|glaxo|abbvie|\babbott\b|regeneron|moderna|biontech|bristol.myers|eli lilly"
     r"|\blilly\b|\bbaxter\b|boston scientific|stryker|medtronic|becton|thermo fisher"
     r"|illumina|\bcigna\b|\bhumana\b|unitedhealth|\baetna\b|\belevance\b|\bcvs\b"
     r"|molina|centene|\bmckesson\b|cardinal health|\bzoetis\b|\bidexx\b"
     r"|\bbiogen\b|\bgilead\b|\bincyte\b|\bexelixis\b|alnylam|\bbiomarin\b|\bseagen\b"
     r"|edwards lifesciences|\balcon\b|\bemory healthcare\b"),
    ("Banking, Finance & Insurance",
     r"\bbank\b|banking|bancorp|bancshares|\bcredit union\b|\bfinancial\b|\bfinance\b"
     r"|asset management|\binvestment|\bsecurities\b|brokerage|hedge fund"
     r"|\binsuranc|\bassuranc|reinsuranc|\bunderwrit|\bactuar|annuit"
     r"|\bmortgage\b|\blending\b|\bpayments?\b|\bfintech\b|\bpayroll\b|\bwealth\b"
     r"|\btrust company\b|\bclearing\b|\bcustod"
     r"|jpmorgan|goldman sachs|morgan stanley|\bcitigroup\b|\bcitibank\b|wells fargo"
     r"|blackrock|blackstone|\bfidelity\b|\bschwab\b|\bvanguard\b|\bnasdaq\b"
     r"|\bvisa\b|mastercard|american express|\bpaypal\b|\bstripe\b"
     r"|capital one|\bdiscover\b|\bsynchrony\b|\bpnc\b|\btruist\b|\bkeybank\b"
     r"|\bhuntington\b|fifth third|northern trust|state street|\bsantander\b"
     r"|\bmoody'?s\b|s&p global|\bmsci\b|\bfactset\b|\bmorningstar\b|\bequifax\b"
     r"|\btransunion\b|\bexperian\b|two sigma|\bcitadel\b|jane street|point72"
     r"|de shaw|bridgewater|worldquant|\baig\b|\bchubb\b|\bmetlife\b|prudential"
     r"|\bmassmutual\b|northwestern mutual|\btravelers\b|\bprogressive\b|\ballstate\b"
     r"|\bgeico\b|liberty mutual|\bnationwide\b|\bhartford\b|\bmarsh\b|\baon\b"
     r"|willis towers|\bmarqeta\b|\btradeweb\b|\bplaid\b|\bbrex\b|\baffirm\b|\bsofi\b"
     r"|\bchime\b|robinhood|\bcoinbase\b|cme group|\blendingclub\b|\bbitgo\b|\bq2\b"),
    ("Aerospace, Defense & Industrial",
     r"aerospace|\bdefen[cs]e\b|\bavionic|\bmissile\b|\bsatellite\b|\bmunition"
     r"|\barmament|\bshipbuild|\bnaval\b|\baircraft\b|\bairframe\b|\bturbine\b"
     r"|\bpropulsion\b|\bordnance\b"
     r"|lockheed|northrop|raytheon|\brtx\b|general dynamics|\bboeing\b|\bspacex\b"
     r"|blue origin|\bl3harris\b|bae systems|\bleidos\b|booz allen|\bcaci\b"
     r"|\bsaic\b|\bperaton\b|\bparsons\b|\banduril\b|\bhoneywell\b|ge aerospace"
     r"|rolls.royce|\bsafran\b|\bthales\b|\bairbus\b|\btextron\b|\bhowmet\b"
     r"|\bheico\b|\btransdigm\b|\bmoog\b|curtiss.wright"
     r"|\bmanufactur|\bindustrial\b|\bmachinery\b|\bsteel\b|\baluminum\b"
     r"|\bchemical|\bpolymer|\bplastics\b|\bcoatings\b|\badhesive|\bcement\b"
     r"|\bpackaging\b|\bceramic|\btextile|\bmining\b|\bmetals\b|\bfabricat"
     r"|caterpillar|\bdeere\b|\bcummins\b|parker hannifin|\bemerson\b|\beaton\b"
     r"|rockwell automation|illinois tool|\b3m\b|\bdupont\b|\bbasf\b|\blinde\b"
     r"|air products|\bppg\b|sherwin.williams|\bhuntsman\b|\bcelanese\b|\bnucor\b"
     r"|\balcoa\b|\bcorning\b|\bwerfen\b|\btenneco\b|\bgrainger\b"),
    ("Energy & Utilities",
     r"\benergy\b|\butilit|electric power|power company|\bpetroleum\b|\brefin"
     r"|\bpipeline\b|\bdrilling\b|\bsolar\b|\brenewable|\bphotovoltaic\b|\bnuclear\b"
     r"|\bhydro\b|\btransmission\b|water district|\bsanitation\b|\bwaste\b"
     r"|exxon|\bchevron\b|conocophillips|totalenergies|schlumberger|\bslb\b"
     r"|\bhalliburton\b|baker hughes|duke energy|\bexelon\b|\bdominion\b"
     r"|southern company|\bnextera\b|consolidated edison|national grid|\bengie\b"
     r"|\bvernova\b|\biberdrola\b|\bvestas\b|first solar|\bsunrun\b|\bsunpower\b"
     r"|\bchargepoint\b|\bfluence\b"),
    ("Engineering, Construction & Real Estate",
     r"\bconstruct|\bcontractor\b|\bbuilders?\b|\bengineers?\b|\bengineering\b"
     r"|\barchitect|\bsurvey(?:or|ing)\b|\bgeotechnic|\bcivil\b|\bstructural\b"
     r"|\bhvac\b|\bplumbing\b|\broofing\b|\bpaving\b|\bconcrete\b|\bdrywall\b"
     r"|\bexcavat|\bdredg|\bmasonry\b|\blandscap|real estate|\brealty\b"
     r"|\bproperties\b|property management|\bhomebuilder|\bfacilities\b|\bjanitorial\b"
     r"|\bjacobs\b|\baecom\b|\bbechtel\b|\bfluor\b|\bkiewit\b|\bskanska\b"
     r"|\bstantec\b|\bwsp\b|\barcadis\b|\bhntb\b|kimley.horn|tetra tech"
     r"|black & veatch|burns & mcdonnell|jones lang|\bcbre\b|\bcushman\b|\bjll\b"
     r"|\bzillow\b|\bredfin\b|\bopendoor\b|\bcostar\b"),
    ("Transport, Logistics & Automotive",
     r"\bautomotive\b|auto parts|\bvehicles?\b|\bmotors?\b|\btires?\b|\bpowertrain\b"
     r"|\bchassis\b|\blogistics\b|\bfreight\b|\btrucking\b|\bshipping\b|\bcourier\b"
     r"|\bwarehous|supply chain|\bfulfillment\b|\bdistribution\b|\bairlines?\b"
     r"|\bairways\b|\bairport\b|\brailroad\b|\brailway\b|\btransit\b|\bmaritime\b"
     r"|\bcruise\b|\bmobility\b|\bfleet\b"
     r"|\btesla\b|\brivian\b|\blucid\b|general motors|stellantis|\btoyota\b"
     r"|\bhonda\b|\bnissan\b|\bhyundai\b|mercedes|volkswagen|\bvolvo\b|\bpaccar\b"
     r"|\bnavistar\b|\bbosch\b|\bdenso\b|\bmagna\b|\baptiv\b|\bborgwarner\b"
     r"|\bgoodyear\b|\bbridgestone\b|\bmichelin\b|\bfedex\b|\bdhl\b|\bmaersk\b"
     r"|\bxpo\b|ch robinson|\bexpeditors\b|union pacific|\bcsx\b|norfolk southern"
     r"|\bbnsf\b|\bamtrak\b|united airlines|american airlines|\bjetblue\b"
     r"|alaska air|frontier airlines|\buber\b|\blyft\b|\bdoordash\b|\binstacart\b"
     r"|\bflexport\b|aurora innovation|\bzoox\b|\bwaymo\b|\bnuro\b|may mobility"),
    ("Retail, Consumer & Hospitality",
     r"\bretail\b|\bstores?\b|\bgrocer|\bsupermarket\b|\bmerchandis|\bapparel\b"
     r"|\bfootwear\b|\bcosmetic|\bjewelr|\bfurnitur|home goods|\bpharmacy\b"
     r"|\brestaurants?\b|\bfoods\b|\bbeverage|\bbrewer|\bdistiller|\bwiner"
     r"|\bhotels?\b|\bresorts?\b|\bcasino|\bhospitality\b|\btourism\b|\bcatering\b"
     r"|\bfranchise|consumer products|consumer goods"
     r"|\bwalmart\b|\btarget\b|\bcostco\b|\bkroger\b|\balbertsons\b|\bpublix\b"
     r"|whole foods|trader joe|\baldi\b|home depot|\blowe'?s\b|\bikea\b|best buy"
     r"|\bmacy'?s\b|\bnordstrom\b|\bkohl'?s\b|\bnike\b|\badidas\b|\blululemon\b"
     r"|under armour|\blevi\b|ralph lauren|estee lauder|\bl'?oreal\b"
     r"|procter & gamble|\bunilever\b|\bcolgate\b|kimberly.clark|\bnestle\b"
     r"|\bpepsico\b|coca.cola|\bkraft\b|general mills|\bkellogg\b|\bconagra\b"
     r"|\btyson\b|\bhormel\b|\bmondelez\b|\bhershey\b|\bstarbucks\b|\bmcdonald\b"
     r"|\bchipotle\b|\bdarden\b|\bmarriott\b|\bhilton\b|\bhyatt\b|\bairbnb\b"
     r"|\bexpedia\b|\bbooking\b|\bwyndham\b|\bcaesars\b|\bwalgreens\b"),
    ("Media, Telecom & Gaming",
     r"\bmedia\b|\bbroadcast|\bpublish|\bnewspaper\b|\bmagazine\b|\bstudios?\b"
     r"|\bentertainment\b|\bfilms?\b|\bmusic\b|\bstreaming\b|\bgames?\b|\bgaming\b"
     r"|\besports\b|\banimation\b|\btelecom|\bwireless\b|\bcellular\b|\bbroadband\b"
     r"|\bcable\b|\badvertis|marketing agency|public relations"
     r"|\bnetflix\b|\bdisney\b|warner bros|\bparamount\b|nbcuniversal|\bcomcast\b"
     r"|\bcharter\b|\bspectrum\b|\bat&t\b|\bverizon\b|t.mobile|\blumen\b|\bdish\b"
     r"|\bdirectv\b|\bsirius\b|\bspotify\b|\bactivision\b|\bblizzard\b"
     r"|electronic arts|\bubisoft\b|take.two|rockstar games|riot games|epic games"
     r"|\bvalve\b|\bzynga\b|\broblox\b|\bnintendo\b|\bxbox\b|new york times"
     r"|washington post|\bbloomberg\b|\breuters\b|\bthomson\b|\bnielsen\b"
     r"|\bomnicom\b|\bwpp\b|\bpublicis\b|\bdigitas\b|\bclarivate\b|\bringcentral\b"),
    ("IT Services & Consulting",
     r"\bconsult|\badvisory\b|systems? integrat|it services|managed services"
     r"|\boutsourcing\b|\bbpo\b|\bstaffing\b|\brecruit|\btalent\b|\bsolutions\b"
     r"|\binfotech\b|professional services"
     r"|accenture|\bdeloitte\b|\bpwc\b|pricewaterhouse|\bkpmg\b|ernst & young"
     r"|mckinsey|boston consulting|\bbain\b|oliver wyman|\balixpartners\b"
     r"|\bslalom\b|\bthoughtworks\b|\bepam\b|\bglobant\b|\bendava\b|\bluxoft\b"
     r"|cognizant|\binfosys\b|tata consultancy|\btcs\b|\bwipro\b|tech mahindra"
     r"|\bmindtree\b|\bltimindtree\b|larsen & toubro|\bmphasis\b|\bzensar\b"
     r"|\bhexaware\b|\bvirtusa\b|\bsyntel\b|\bniit\b|\bcapgemini\b|\batos\b"
     r"|\bdxc\b|\bunisys\b|\bcgi\b|\bgenpact\b|\bwns\b|\bconcentrix\b"
     r"|\bteleperformance\b|\bcompunnel\b|\bcitiustech\b|\bmarlabs\b|\bprokarma\b"
     r"|ust global|\bkforce\b|\brandstad\b|\badecco\b|\bmanpower\b|robert half"
     r"|kelly services|insight global|\bteksystems\b|apex systems|\beteam\b"
     r"|\brsm\b|grant thornton|\bbdo\b|\bcrowe\b|\bprotiviti\b|zs associates"
     r"|michael page|\bjacobs civil\b"),
    ("Software & Internet",
     r"\bsoftware\b|\bsaas\b|\bplatform\b|\bcloud\b|\banalytics\b|\bdatabase\b"
     r"|\bdevops\b|cybersecurity|artificial intelligence|machine learning"
     r"|\binternet\b|\bdigital\b|\bcrm\b|\berp\b"
     r"|\bmicrosoft\b|\bgoogle\b|\balphabet\b|\bamazon\b|\baws\b|\bapple\b|\bmeta\b"
     r"|\bfacebook\b|\bibm\b|\boracle\b|\bsalesforce\b|\bsap\b|\badobe\b|\bintuit\b"
     r"|\bservicenow\b|\bworkday\b|\bsnowflake\b|databricks|\bpalantir\b|\bsplunk\b"
     r"|\bmongodb\b|\belastic\b|\bconfluent\b|\bhashicorp\b|\bdatadog\b|\bdocker\b"
     r"|\bgithub\b|\bgitlab\b|\batlassian\b|\bnotion\b|\bfigma\b|\bcanva\b|\bslack\b"
     r"|\bzoom\b|\bdropbox\b|\bokta\b|\bauth0\b|\bcloudflare\b|\bakamai\b|\bfastly\b"
     r"|\bdigitalocean\b|\bvmware\b|\bcitrix\b|\bnutanix\b|\bcisco\b|\bjuniper\b"
     r"|\barista\b|palo alto|\bfortinet\b|\bcrowdstrike\b|\bzscaler\b|\bsentinelone\b"
     r"|\bsymantec\b|\bmcafee\b|\brapid7\b|\btenable\b|\btwilio\b|\bshopify\b"
     r"|\bsquarespace\b|\bwix\b|\bhubspot\b|\bzendesk\b|\bfreshworks\b|\basana\b"
     r"|\bsmartsheet\b|\bdocusign\b|\bcoupa\b|\bworkiva\b|\bveeva\b|epic systems"
     r"|\bcerner\b|\bopenai\b|anthropic|scale ai|hugging face|\bcohere\b"
     r"|\bnetsuite\b|\bautodesk\b|\bansys\b|\bptc\b|\bdassault\b|\bunity\b"
     r"|\bmathworks\b|\bwolfram\b|\bteradata\b|\bcloudera\b|\binformatica\b"
     r"|\bsamsara\b|\bverkada\b|\btoast\b|\bcheckr\b|\bramp\b|\bvanta\b|\breplit\b"
     r"|\bcursor\b|\bpinterest\b|\bsnap\b|\breddit\b|\blinkedin\b|\btwitter\b"
     r"|\btiktok\b|\bbytedance\b|\bguidewire\b|\banaplan\b|\bcelonis\b|new relic"
     r"|\bsaviynt\b|\bsift\b|\baltair\b|\bnvidia\b|\beightfold\b"),
]
_KEYWORDS = [(s, re.compile(rx, re.I)) for s, rx in KEYWORDS]

# core.is_cap_exempt is one bucket; a university and a hospital are both lottery-exempt but
# they are not the same job market, so split on which kind of name it is.
_HOSPITAL_RE = re.compile(
    r"hospital|health system|healthcare system|medical cent(?:er|re)|\bclinic\b"
    r"|cancer (?:cent(?:er|re)|institute)|\bhealth\b|\bmedical\b|\bnursing\b|\bhospice\b",
    re.I)

# Corrections, written as display names and normalized at load so nobody has to hand-compute
# a core.norm_company key. An entry earns its place because a rule got a PROMINENT company
# wrong -- one with live postings or real filing volume. The long tail is NOT pre-populated;
# it stays Unsorted and the page says so. `--check` is what holds this line.
_CURATED_LISTS = {
    "Software & Internet": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        "C3 AI", "Tyler Technologies",
        # --check named these prominent-and-Unsorted after the 2026-09-02 adoption
        # run put 269 new employers into the universe.
        "84.51", "Box",
        "Amazon", "Amazon Web Services", "Apple", "Microsoft", "Meta", "Meta Platforms",
        "Alphabet", "eBay", "ADP", "Cerner", "VMware", "Salesforce.com", "Cisco Systems",
        "Oracle America", "Twitter", "Juniper Networks", "Eightfold AI",
        "Fluidstack", "CRUSOE", "Speechify", "Fivetran", "Pure Storage", "Appian", "Mirantis",
        "CoreWeave", "Delinea", "Commure", "Bentley", "HARVEY", "Gusto", "Socure", "Benchling",
        "Luma AI", "Dragos", "Astronomer", "Ridgeline", "Trace3", "Headway", "ACI Worldwide",
        "Ripple", "Extreme Networks", "The Trade Desk", "Cribl", "AlphaSense", "SentiLink",
        "Tanium", "Zeta Global", "Justworks", "Avalara", "PathAI", "Deepgram Inc", "Cvent",
        "Upstart", "Netskope", "Red Hat", "Rubrik", "CLEAR", "LendingTree", "Thumbtack",
        "Enova", "Quora", "Qualtrics", "Pivotal", "OneTrust", "Zuora", "Medallia", "UiPath",
        "Model N", "SailPoint Technologies", "Nextdoor", "athenahealth", "Cohesity",
        "Groupon", "Blue Yonder", "F5", "CDK Global", "Cornerstone Ondemand INC", "Magic Leap",
        "Manhattan Associates", "Credit Karma", "Sabre", "Asurion", "Avant", "Plenful",
        "Metropolis", "Nice", "Lseg", "Ssctech", "StubHub", "CarGurus", "Yelp", "Indeed",
        "ServiceTitan", "Upwork", "Traba",
        "Whoop", "Formlabs", "Shield AI", "Skydio", "Axon", "Apptronik", "Agility Robotics",
        "Torc Robotics", "Motional", "Wing", "SimpliSafe", "Tatari", "Oneapp", "Pacelabs",
        "Ejta", "Hdpc", "Eswt", "Saama Technologies LLC", "Visionet Systems", "Infogain",
        "Intraedge INC", "Nagarro INC", "Coforge Limited", "Brillio LLC", "Natsoft",
        "Persistent Systems Limited", "Birlasoft INC", "Synechron", "Cyient INC",
        "Htc Global Services", "Quadrant Technologies", "Centraprise", "DGN Technologies",
        "RJT Compuquest", "Lead IT", "iTech", "Vastek", "First Tek", "Humac INC", "iPivot",
        "Servesys", "Raas Infotek", "Gp Technologies", "Asta CRS", "Kairos Technologies",
        "IPolarity LLC", "Antra, Inc", "YASH Technologies", "Mican Technologies",
        "Orion Innovation", "World Wide Technology", "Tencent America LLC", "Fca",
        "Ntt Data Services", "NTT DATA", "Exlservice.Com LLC", "EXL Service", "Conduent",
        "Fis Management Services", "Fiserv", "Paychex", "NetApp", "Y Combinator Work at a Startup",
        "Y Combinator's Work at a Startup",
    ],
    "IT Services & Consulting": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        # "swift" here is SWIFT TECHNOLOGIES INC, an IT staffing firm -- NOT the interbank
        # messaging network and not Swift Transportation. norm_company strips "TECHNOLOGIES",
        # so all three collapse to one key; measured, this is the only one in the corpus.
        "Intellectt Inc", "SWIFT TECHNOLOGIES INC",
        # --check named these prominent-and-Unsorted after the 2026-09-02 adoption
        # run put 269 new employers into the universe.
        "APLOMB Technologies", "Circana", "Forge Group",
        "Michael Page", "EY", "KBR", "ICF", "Serco", "Guidehouse", "CBIZ", "Eide Bailly",
        "CSC", "Maximus", "Quest Global", "Populus Group LLC", "Grandison Management",
        "Bureau Veritas", "Pearson", "HCL Technologies", "Wood Group",
        "Accion Labs", "CDW", "Congensys CORP", "Gallup", "Infodat", "Inrika",
        "IntelliPro Group", "Jean Martin INC", "Kaar Technologies INC", "Northstar Group INC",
        "Saturn Tech LLC", "Saxon Global", "Softpath System LLC", "Sriven Systems Inc",
    ],
    "Semiconductors & Hardware": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        "Cerebras Systems", "Super Micro Computer, Inc.",
        "Nvidia", "Intel", "Dell", "Dell EMC", "HP", "Hewlett Packard Enterprise", "Lenovo",
        "Samsung", "Amat", "EMC", "Qualcomm", "Broadcom",
        "Teradyne", "Jabil", "Celestica", "FormFactor, Inc.", "Keysight Technologies",
        "Analogdevices", "Advanced Micro Devices", "Flex", "Logitech", "Zebra Technologies",
        "Arm", "Te Connectivity", "Agilent Technologies", "Samsung Research America",
        "Samsung Electronics America", "Nokia of America", "Mercury", "LG Electronics",
        "Cricut", "Titan",
    ],
    "Banking, Finance & Insurance": [
        "PayPal", "Capital One Services", "Barclays", "Barclays Services", "Barclays Capital",
        "New York Life", "State Farm", "Invesco", "Unum", "Susquehanna International Group",
        "Aflac", "Assurant", "USAA", "Transamerica", "Raymond James & Associates",
        "Freddie Mac", "H&R Block", "Western Union", "Akuna Capital", "Oportun", "RBC",
        "Citi", "Intercontinental Exchange Holdings", "Equinox",
        "Block", "Milliman", "Plymouth Rock",
    ],
    "Healthcare, Pharma & Biotech": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        # A CLINICAL REFERENCE LAB, unrelated to Arup the engineering consultancy under
        # Engineering below -- one letter-case apart, distinct normalised keys, so exact-match
        # CURATED holds both correctly where a widened keyword could not.
        "ARUP Laboratories",
        # --check named these prominent-and-Unsorted after the 2026-09-02 adoption
        # run put 269 new employers into the universe.
        "Align Technology",
        "Philips", "Roche", "Johnson & Johnson", "Anthem",
        "Danaher", "IQVIA", "Medpace", "BD", "Aegis Therapies", "Novo Nordisk, Inc.",
        "Eurofins", "Zimmer Biomet", "Zimmer", "Teleflex", "Labcorp", "Revolution Medicines",
        "DaVita", "Takeda", "Natera", "Lonza", "Dexcom", "Insulet Corporation",
        "Intuitive Surgical Operations", "Medline Industries", "Getinge", "Hanger",
        "AccentCare", "Pristine Rehab Care", "Lila Sciences", "Caremark", "Optum Services",
        "Janssen Research & Development", "Sigma-Aldrich",
        "Masimo", "Resmed",
    ],
    "Universities & Research": [
        "Northwestern", "Dallas Independent School District", "Harmony Public Schools",
        "Texas A&M Agrilife Research", "Triad National Security", "The Devereux Foundation",
        "Open Avenues Foundation",
    ],
    "Hospitals & Health Systems": [
        "Providence", "OhioHealth", "UPMC",
    ],
    "Aerospace, Defense & Industrial": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        "Stanley Black & Decker, Inc.", "Mohawk Industries", "Pella Corporation",
        "General Electric", "Siemens", "Johnson Controls", "3M", "Corning", "Honeywell",
        "Carrier", "Hubbell", "AMETEK", "ANDRITZ", "Wabtec", "Flowserve", "Ecolab",
        "EnerSys", "Franklin Electric", "Regal Rexnord Corporation", "Timken", "Victaulic",
        "Airgas", "Gates Corporation", "Hunter Douglas", "Andersen Corporation", "Franke",
        "Rockwell Collins", "Dematic", "Brunswick", "FMC Corporation", "Uline",
        "United Rentals", "Lennox International", "Redwood Materials",
        "AST SpaceMobile", "Danfoss", "Joby Aero", "Saint-Gobain",
    ],
    "Energy & Utilities": [
        "Kinder Morgan", "Entergy", "PacifiCorp", "Ameresco", "Republic Services",
        "United Site Services", "Loenbro",
        "Itron",
    ],
    "Engineering, Construction & Real Estate": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        "Arup", "Bowman", "Introba",
        "Sundt", "M.C. Dean, Inc.", "Group PMX", "Thornton Tomasetti", "WillScot",
        "CubeSmart", "Public Storage", "Safelite", "ECS Limited",
        "Stv",
    ],
    "Transport, Logistics & Automotive": [
        "Tesla", "Ford Motor", "Uber", "Lyft", "Rivian Automotive", "Zoox",
        "ZF", "Lear Corporation", "Dana", "Nikola", "Faraday Future", "Delta Air Lines",
        "Kuehne+Nagel", "Ingram Micro", "Bunge", "Archer Daniels Midland", "Maplebear",
        "Coupang", "Chewy", "Wayfair",
    ],
    "Retail, Consumer & Hospitality": [
        # --check named these prominent-and-Unsorted on 2026-09-08; the rebuild is gated on it.
        # "compass" is Compass Group, food service -- NOT Compass the real-estate brokerage,
        # which normalises to the same key. Measured: only one spelling in the corpus today.
        "Compass Group", "Shipt", "The TJX Companies, Inc.",
        # --check named these prominent-and-Unsorted after the 2026-09-02 adoption
        # run put 269 new employers into the universe.
        "Williams-Sonoma",
        "Walgreens", "Walmart", "Walmart Global Tech", "Wal Mart Associates",
        "Safeway", "Staples", "Cintas", "Red Bull", "Skechers", "Chobani", "Sephora",
        "Meijer", "Dollar general", "Autozone", "Peloton", "Bose", "Juul Labs",
        "Stitch Fix", "Jack Link's Protein Snacks", "GROWMARK", "arrivia",
        "Churchill Downs Inc.", "National Veterinary Associates", "Avery Dennison",
        "Rocket", "Garmin",
        "Mcdonalds",
    ],
    "Media, Telecom & Gaming": [
        # --check named these prominent-and-Unsorted after the 2026-09-02 adoption
        # run put 269 new employers into the universe.
        "Aristocrat",
        "Netflix", "Sony", "Oath", "Yahoo",
        "FanDuel", "IGT", "FloSports Inc.", "Genesis",
        "Audible", "iHeartMedia",
    ],
}

# The long-tail sweep, kept separate from the corrections above because its provenance is
# different: these were read off `--report` and classified by hand, in bulk, after measuring
# that no keyword rule could reach them. That measurement is the reason this block exists at
# all -- twelve candidate anchor patterns (ai, robotics, security, dental, schools, county,
# staffing, food, industrial...) were tried against the 916 unsorted names and the BEST of
# them matched 15. The tail is brand names with no shared industry vocabulary, so there is
# nothing to generalise and the only honest options were a list or an apology.
#
# What is deliberately NOT here: names that are scraper artifacts rather than employers
# ("Mon1026Monoh", "Fa Exhh Saasfaprod1", "Smart Apply Test Company", "Hdow", "Hcxs"), and
# genuinely unrecognisable one-offs. Those stay Unsorted, which is the correct answer for
# them -- see the data-quality note in the module docstring.
_CURATED_TAIL = {
    # 2026-08-31 refresh: _prominent() admits any employer with h1b >= 100, so the refreshed
    # counts pushed 14 employers over that line and `--check` failed until they were sectored.
    # The ambiguous ones were resolved from their BOARD URL, never their name -- "ICE" is
    # careers.ice.com, i.e. Intercontinental Exchange (which owns the NYSE, and already sits
    # in the block above as "Intercontinental Exchange Holdings") and emphatically NOT
    # Immigration and Customs Enforcement; "Coastal" is Coastal Community Bank; "David" is the
    # protein-bar startup. Sonos follows Bose/Garmin/Peloton into Retail rather than Hardware,
    # matching how this file already treats consumer audio.
    "Software & Internet": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Gigamon", "ZIPRECRUITER INC", "VISTEX INC", "Glean",
        "Lookout", "Integral Ad Science", "Imprivata", "SeatGeek",
        "MenuSifu Inc.", "Celigo", "Airbyte", "Bazaarvoice",
        "Ocrolus", "Ping Identity", "ComplianceQuest", "Arena",
        "Boson AI", "Sovrn", "OpenX", "Cognition",
        "Middesk", "Boulevard", "Ladders", "Doppel",
        "Clockwork Systems", "AdvancedMD", "Incident IQ", "Intelerad",
        "BlueSight", "Notable", "Outtake", "Ditto",
        "Cinder", "LendingPad", "Alchemer",
        # + adopted 2026-08-31, ranked-sponsor probe batch 2 (federal spellings).
        "AMDOCS INC", "BOOMI LP", "SOPHOS INC",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Fieldguide", "Otter.ai", "CompanyCam", "Fingerprint", "BeyondTrust", "NetDocuments", "Scribe", "Hightouch",
        "Veritone", "ClickUp", "Bubble", "Epicor", "Merge", "Instawork", "Cloudbeds", "Nexthink", "Entrata", "You.com",
        "Endor Labs", "Exiger", "Edmentum", "Imagine Learning", "Art of Problem Solving", "RealPage Inc",
        "Office Ally", "Pylon", "Vast.ai", "Unwrap", "Authorium", "Kargo", "Blitzy", "Vendelux", "Emergence AI",
        "Rillet", "Nabla", "Aptos Labs", "Taxbit", "Luminai", "Salient", "Campus", "AfterQuery", "Warp", "Quilt",
        "RF-SMART", "OpenEye", "Beacon AI", "Vorto", "Sesame", "Gradera", "Revivn", "Atticus", "turing", "Air", "FacilityOS",
        "OneCrew", "Pryzm", "Agave", "Air Apps", "Bobyard", "Lab37", "Odyssey",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "Klaviyo", "ZoomInfo",
        "CoVar", "Kobie", "Engine", "Evolve",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "OpenGov Inc.", "Allvue Systems", "Planview", "Temporal Technologies", "Entrust",
        "Rocket Lawyer", "Infotrust", "TRM Labs", "Anrok", "WalkMe", "Camunda", "DailyPay",
        "Kafene", "Valon", "Acronotics", "FloQast", "FullStack", "Kochava", "LeanData",
        "Reality Defender", "Trulioo", "6sense", "Abaka AI", "Artie", "Ashby", "Assured",
        "AWeber", "Bitwarden", "Braintrust", "Brave", "Brellium", "Brigit", "CaptivateIQ",
        "Cobot", "Coderio", "Coinflow", "Compa", "ConcertAI", "ControlUp", "Cordance",
        "Credit Genie", "DeepMind", "Doxim", "EagleView", "Esri", "EvolutionIQ", "Finix",
        "FloatMe", "Genius AI", "GetWhys", "Glance", "Gyde", "Handoff", "Handshake", "Hudl",
        "jamf", "Keeper Security, Inc.", "KUNGFU.AI", "Legora", "Lightfield", "Lightning AI",
        "Metaview", "NameSpace", "Oowlish", "Parloa", "Pragmatike", "PrizePicks", "Prospyr",
        "Reducto", "Rundoo", "Simplesense", "SkySlope", "Solution Design Group", "Storable",
        "Striveworks", "Sustainment", "Swayable", "TensorWave", "Teraswitch", "Upside", "VTS",
        "VulnCheck", "WellBeam", "WireScreen", "Zapier", "Novig", "Tarro", "OFFICE HOURS",
        "Everforth", "Everforth ECS", "Simple Science Inc.", "MetaHorizon", "Linea Labs",
        "HUMAN", "Aurelian", "CBI", "Logic, Inc.", "Fortreum", "Quarterhill Inc.",

        # added 2026-08-24, see the note above
        "LivePerson", "VERISIGN",
        "QuinStreet", "Provectus", "Recorded Future", "ClickHouse", "LangChain", "OpenText",
        "STANDARD BOTS COMPANY", "Faire", "Braze", "Blackbaud", "Instructure", "IXL Learning",
        "Hopper", "Duolingo", "phData", "Bandwidth", "GoFundMe", "Moloco", "Zocdoc", "Sentry",
        "Truveta", "Lambda", "Workstream Technologies", "AssistRx", "webAI", "Criteo",
        "Illumio", "Life360", "Amplitude", "Cambridge Mobile Telematics", "Otter", "Postman",
        "Backblaze", "brightwheel", "Eulerity", "Mill", "Rippling", "WRITER", "YipitData",
        "Udemy", "Vertafore", "Outreach", "OpenTable", "AppLovin", "Five9", "JFrog", "StockX",
        "VideoAmp", "Smarsh", "Centerfield", "Symplicity", "Aptean", "Suvoda", "Pattern",
        "Snorkel AI", "Obsidian Security", "Pendo", "Fictiv", "Semperis", "Smartrent",
        "Carta", "Crexi", "Decagon", "Drata", "Eclinicalsolutions", "Gopuff", "Gruve",
        "Reflection AI", "Spscommerce", "TARANIS", "via", "Waymark", "Addepar", "Axle",
        "InterSystems", "PubMatic", "Finastra", "Upgrade", "Wealthfront", "Alarm.com",
        "New Era Technology", "Mindbody", "Discord", "Ensono", "Viant Technology", "Envoy",
        "Credible", "DigiCert", "Kyndryl", "Nextiva", "Vestmark", "Sezzle", "Iterable",
        "Endpoint Clinical, Inc", "NetSpend", "Halvik", "Freenome", "Taskrabbit", "Workato",
        "Alpha Omega Integration", "WellSky", "Auctane", "SingleStore", "Sonatus",
        "Demandbase", "Forcepoint", "Instabase", "Starburst", "Ubiquiti", "ACV Auctions",
        "Taboola", "Wasabi Technologies", "project44", "RxLogix", "Druva", "Phantom AI",
        "Avetta", "DevRev", "GoodLeap", "Samba TV", "Newsela", "Nextech", "Everlaw",
        "Darktrace", "Algolia", "ChowNow", "Hi Marley", "Lessen", "Radar", "Worldpay",
        "Knit", "Substack", "Gather AI", "Geotab", "Metrostar Systems", "SoftWriters",
        "Solera", "VALIANTYS", "Alchemy", "Eventual", "Flash", "HealthVerity", "Pacvue",
        "Park Place Technologies", "SumUp", "Argano", "BuildOps", "Empower AI Inc.",
        "Customcomputerspecialists", "GoDaddy", "MERCOR", "PermitFlow", "SafetyCulture",
        "Stedi", "Telos Corporation", "UpdateMe", "Lacework", "Axiom Technologies",
        "DoubleVerify", "Mapbox", "Verifone", "Poshmark", "Sysdig", "Reltio", "Patreon",
        "ActiveCampaign", "Disqo", "Lattice", "PagerDuty", "Aera Technology", "Bloomreach",
        "Zimperium", "Netcracker", "Deposco", "Lendbuzz", "Mercari Inc.", "Certara",
        "Cyngn", "Elemica INC", "Kaseya", "Strava", "Doximity", "OVERJET", "Syndigo",
        "BetterUp", "Anyscale", "Koddi", "Arkose Labs Holdings INC.", "MobilityWare",
        "campfire", "Cresta", "Fortanix INC", "Resilience", "Locus Robotics", "Redis",
        "Motive", "Testingxperts", "Legion", "MinIO", "Neo4j", "Quantifind", "Gorgias",
        "Jumio Corporation", "JumpCloud", "Doxel", "Nova Credit", "Canidium", "Deel",
        "HG Insights", "Invisible Technologies", "Nsight", "Amperity", "armis", "Canonical",
        "Dataiku", "Innovid", "PickTrace", "Solovis", "Topaz Labs", "Payactiv", "ProcDNA",
        "Velosio", "Aircall", "Applicantz", "Invoca", "Kikoff", "Snappr", "Viz.Ai",
        "Airwallex", "Cardless", "Meter", "Oscilar", "Vercel", "15Five", "Atlan, Inc.",
        "Etched", "Ideagen INC", "PayStand", "Typeface", "WorkOS", "100ms Inc", "AcuityMD",
        "MaintainX", "MANYCHAT INC", "Ownwell, Inc.", "Parspec, Inc.", "Acceldata",
        "AfterShip", "Articul8", "Baya Systems", "Calendly", "Duetti", "Equativ", "Forter",
        "FurtherAI", "Glimpse", "Guardsquare", "HockeyStack", "Influ2", "Jellyfish",
        "Liquid Ai", "Mesh", "Mintlify", "Mirakl, Inc", "onbe", "Paraform", "Perplexity",
        "Planhat", "Pliancy", "Reevo", "Retell AI", "Runwise", "Semgrep", "Shopmonkey",
        "Spinnaker Support", "Talentful Inc", "Triple Whale Inc", "Trustpilot",
        "Tutor Intelligence", "Wisdom Ai", "Voltai", "Serve Robotics", "Databento Inc.",
        "GuidePoint Security", "CPI Security", "Sphere", "Machine Intelligence",
        "IT Automation", "Physical Intelligence", "Isomorphiclabs", "Imbue", "Censys Technologies Corporation",
        # second pass over what the first sweep left behind
        "Stepful", "KnowBe4", "Mashgin", "Sereact", "Tobor Robot Corporation", "Togetherai",
        "BrainCo Technologies, Inc.", "Cockroach Labs", "Clever Inc.",
        "Virtual Reality Technologies", "Augmented Reality Technologies", "Aquabyte",
        "Sharebite Inc", "Paperclip Inc", "Felix Technologies Inc", "Future Secure AI",
        "Qurrent", "Garage Technologies, Inc.", "Y Combinator",
    ],
    "IT Services & Consulting": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "MAGANTI IT RESOURCES LLC", "RAKS GROUP LLC", "BridgeNexus Technologies Inc", "Enexus Global Inc.",
        "Further", "Accellor", "PingWind Inc.", "Toptal",
        "Fearless", "Appnovation", "AgileEngine",
        # + adopted 2026-08-31, ranked-sponsor probe batch 2 (federal spellings).
        "ADVITHRI TECHNOLOGIES LLC", "CAPRUS IT INC", "EMONICS LLC", "GALAX-ESYSTEMS CORPORATION",
        "INTELLYK INC", "ISPACE INC", "ITVORKS INC", "KANAP SYSTEMS LLC", "KYYBA INC", "MAVEN COMPANIES INC",
        "MILLENNIUM INFO TECH INC", "ORPINE INC", "PEOPLE TECH GROUP INC", "RAPIDIT INC", "SOFTWORLD TECHNOLOGIES LLC",
        "SWANKTEK INC", "TACHYON TECHNOLOGIES LLC", "TECHDATA SERVICE COMPANY LLC", "TECHNOGEN INC",
        "VITESSE GROUP INC", "WEBILENT TECHNOLOGY INC", "XENON INFOTEK INC", "XORIANT CORPORATION",
        # + adopted 2026-08-31 from the ranked-sponsor probe. FEDERAL spellings, because that
        # is the name adopt_everify_boards stored the board under.
        "EPITEC INC", "HCL AMERICA INC",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Nortal", "Applied Information Sciences", "Computer Services", "Diversified Services Network",
        "Phenom",
        "Raft", "AVI-SPL",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "A-TEK Inc", "ASRT, Inc.", "Tech Army, LLC", "techolution", "Softchoice",
        "Computacenter", "ePlus inc.", "Dev Technology Group, Inc.", "DOMA Technologies",
        "AnaVation LLC", "Cayuse Holdings", "CBTS", "ClearEdge", "Covista", "INA Solution",
        "SRM Technologies", "Solvo Global", "Zensa LLC", "hatch IT", "Aquent", "Terac",
        "USfalcon", "Wyetech, LLC", "SOSi", "Goldbelt", "Akima",
        "Alaka`ina Foundation Family of Companies", "Intrado", "J.S. Held", "Telligen",
        "SFDS LLC", "Post & Parcel, LLC", "Decima International",

        # added 2026-08-24, see the note above
        "ALTEN Technology USA", "REI Systems", "TriNet",
        "eClerx", "Capco", "Analytic Partners", "Bounteous", "Riveron", "Aprio",
        "LinTech Global, Inc.", "NSD International", "HyerTek Inc.", "Redapt inc",
        "THEMESOFT", "Prospance Inc", "Aries Computer Systems, Inc.", "Standish Management",
        "Productive Resources", "Dentons", "Jensen Hughes", "Credence",
        "Technology Service Professionals, Inc", "Shuttleworth LLC", "Tms Llc", "Talan",
        "Pringle Technologies Inc", "Systems Technology & Research", "Torch Technologies",
        "Andromeda Systems Incorporated", "ASRC Federal", "Avicado", "BC Forward", "Bowhead",
        "CBInc.", "Insperity", "Magnakom", "Qualdoc", "ProvisHR", "West Cary Group",
        "Saransh INC", "My3Tech", "Comprobase INC", "Venturesoft Global", "ITG Technologies",
        "Insight Direct", "DataSync", "White Collar Technologies", "VRN Technologies",
        "Iqlogg", "ITsutra", "SEGULA Technologies", "Sunsoft Services INC",
        "Intelligroup USA LTD", "Mobiquity INC", "Smart IT Frame LLC", "TekHQS",
        "KPI Partners", "Saras America", "Lgc Global INC", "Thirthasoft", "Vaco",
        "Artifint Technologies", "Blue Spire INC", "Harmonic Group Inc", "Serigor INC",
        "Reveille Technologies,Inc", "FNS", "Ivy Enterprises",
        "Intelligent Automation Technology INC", "Code and Theory", "Groksys LLC",
        "Aceintegrator", "Rpa Technologies", "Samsung SDS America", "Atser", "CED Systems",
        "Openmind Technologies Inc", "Jayes Tech LLC", "Cloudeqs LLC", "Datazymes Inc.",
        "Kernel Technologies, Inc.", "REPLY", "Mindgruve", "Armadin Inc",
        "Enterprise Solution Partners LLC", "Exential Us INC", "Fiducial Inc.",
        "Ibx LLC", "Mobicloud LLC", "Movate INC", "Msr Technology Group",
        "Ntt Data Americas", "Plante Moran", "Qse7 LLC", "Smac Apps LLC", "URSUS INC",
        "Victoriam & CO Americas CORP", "Insight Enterprises", "Eight Eleven Group LLC",
        "Rapid Eagle Inc", "Twin Peaks Inc", "S R International Inc", "Intellibee Inc",
        "PERIMATICS", "EisnerAmper", "DuCharme, McMillen & Associates, Inc.", "GFT",
        "Ryan", "Baird", "Advanced Technology Services", "CARIAN", "Quanta Technology",
        "Halvik", "Stark Tech", "Nysarc INC Essex County Chapter", "SERVAL", "ZS",
        "PMG", "Brainlabs", "FleishmanHillard", "Havas Pr North America INC",
        "Jack Morton Worldwide", "Vinfinities Corp", "Groupultra Limited",
            "Honigman LLP", "Davis Wright Tremaine", "Moore & Van Allen",
    ],
    "Semiconductors & Hardware": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "CelLink", "TTI, Inc.",
        # + adopted 2026-08-31, ranked-sponsor probe batch 2 (federal spellings).
        "COGNEX CORPORATION", "ENTEGRIS, INC.",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "Tenstorrent",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Supermicro", "QTS Data Centers", "ServerFarm", "EchoStar Corporation",

        "Astera Labs", "Lightmatter", "Graphcore Technologies Inc.", "Onto Innovation",
        "Bourns", "SK Hynix America", "X-FAB", "TTM Technologies", "Excelitas Technologies",
        "Littelfuse", "Coherent Corp.", "Eliyan", "Keyence", "Lite-On, Inc.", "Razer",
        "Owl Labs", "Ooma, Inc.", "Rocket EMS", "ADDITEC", "Draper", "PsiQuantum",
        "Nusano", "Lumilens Inc.", "Sungrow", "Gentex Corporation", "TD SYNNEX",
        "Ingram Micro", "Lgelectronics", "Netradyne", "Zello",
            "TENSORDYNE, INC",
    ],
    "Banking, Finance & Insurance": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Deluxe Corporation", "Veterans United Home Loans", "Mapfre", "Trepp, Inc.",
        "SitusAMC", "Alter Domus", "MoonPay", "Burford Capital",
        "Column", "Gravie",
        # + adopted 2026-08-31, ranked-sponsor probe batch 2 (federal spellings).
        "TOWER RESEARCH CAPITAL LLC",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Adyen", "DriveWealth", "SageSure", "Crum & Forster", "Hiscox", "Acrisure", "Rockefeller Capital Management",
        "GLOBAL X ETFs", "Lincoln International", "NMI", "Kasheesh",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "ICE", "Early Warning Services", "Polymarket", "Coastal",
        "Range",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Credit Acceptance", "Betterment", "Nubank", "Circle", "sFOX", "Jump Trading",
        "tastytrade, Inc.", "SelectQuote", "Kemper", "Frost", "VantageScore", "SRS Acquiom",
        "United Educators", "Point C",

        "Scotiabank", "ALTRUIST", "PayJoy", "Stashinvest", "Berkadia", "Arcesium", "Lower",
        "Clear Street", "Fireblocks", "Parafin", "Convera", "William Blair & Company",
        "SMBC US", "PDT Partners", "Vestwell", "Capital Farm Credit", "DTCC USA",
        "First American", "Sezzle", "Protective", "Athene", "Hudson River Trading",
        "Sunrise Futures", "Capsule", "Munich Re America Services INC", "Stifel",
        "Thrivent", "Valkyrie Trading", "Tikehau Capital", "Quanata", "Vesta",
        "Welltower, Inc", "Prologis", "Wheels Up", "Nitra, Inc.", "Payactiv",
        "Blue Cross Blue Shield of Mississippi", "HealthPartners", "Medica Services Company LLC",
        "Tensec", "Alloy", "Tebra",
            "Sedgwick", "ServiceLink", "OKX", "FalconX", "Red Cell Partners", "FM",
    ],
    "Healthcare, Pharma & Biotech": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "GENSCRIPT USA INC", "Butterfly Network", "Everest Clinical Research", "Apella",
        "Prolaio", "HistoSonics",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Abridge", "Everlywell", "SmithRx", "Impiricus", "Octave", "Emmes Group", "HealthPRO Heritage", "Alteva RCM",
        "Envista Holdings",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "Intuitive",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "U.S. Renal Care", "Medica", "Talkspace", "PatientPoint", "Genalyte, Inc.",
        "Flagship Pioneering, Inc.", "Prokidney", "Octapharma Plasma, Inc.", "Perrigo",
        "Siemens Healthineers", "Haleon", "Beacon Biosignals", "Sendabiosciences",
        "IntegriChain", "Ennoble Care", "Florida Blue", "Imagen Dental Partners",
        "Inizio Evoke", "International SOS", "Curaleaf",

        # added 2026-08-24, see the note above
        "Inovalon",
        "GSK", "Aledade", "ClinChoice", "CONMED", "UCB", "Cordis", "Cambrex", "Somatus",
        "Generate Biomedicines", "WelbeHealth", "Freedom Care", "NexHealth", "NOCD",
        "Autism Learning Partners", "Fortrea", "Ginkgo Bioworks", "Arthrex",
        "Dentsply Sirona", "Celerion", "Ocular Therapeutix, Inc.", "Straumann Group",
        "argenx", "Absci", "Permobil", "Vaxcyte", "LivaNova", "American Regent",
        "Brainlab", "Inotiv", "MicroAire Surgical Instruments", "Texas Oncology",
        "Familia Dental", "Ati Holdings", "Eisai", "Astellas", "Aspen Dental",
        "42 North Dental", "ChenMed", "Akoya", "Henry Schein", "Kenvue", "Certara",
        "Forge Biologics", "Pivot Bio", "Gator Bio", "Altos Labs", "Advanced Physical Therapy",
        "Clarkson Eyecare", "hear.com", "Integrated Dermatology", "Metro Vein Centers",
        "OneOncology", "Septerna", "Plasmidsaurus", "Headlands Research, Inc",
        "Cellares", "Centivo", "Resonetics, LLC", "Neptune Technology Group", "Nextech",
        "Comfort Keepers", "ConvenientMD", "EVG Specialty Network", "Solace Care",
        "Lone Star Circle of Care", "Somatus", "Modern Animal", "Vetcor", "ABS Kids",
        "The Stepping Stones Group", "Elsevier", "Richmond Children Center", "Aledade",
        "Sleep Number Corporation", "Wider Circle", "Tia", "Air Methods", "Fugro",
            "Starkey", "ATCC", "Neuralink", "BillionToOne", "Clarioclinical", "Heidihealth.Com.Au",
    ],
    "Hospitals & Health Systems": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "INTERNATIONAL QUALITY HOMECARE COR", "Akumin", "Vinfen",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "AdventHealth", "AmeriHealth Caritas", "RadNet", "STERIS",

        "PruittHealth", "TriHealth Inc.", "HonorHealth", "CentraCare", "MaineHealth",
        "CommunityCare", "Pathways Inc", "Nysarc INC Essex County Chapter",
        "AccentCare", "Aegis Therapies", "Pristine Rehab Care",
    ],
    "Universities & Research": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "PALNI INC",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Aegis Ventures",

        "UCLA", "Virginia Tech", "Wgu", "Administrators of the Tulane Educational Fund",
        "New Jersey Innovation Institute, Inc.", "Encyclopaedia Britannica",
        "International Student Exchange Programs", "Improve Your Tomorrow",
        "The Nature Conservancy", "Stand Together", "ActBlue Inc.", "Draper",
    ],
    "Government & Public Sector": [
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "State of New Mexico", "City of Charleston",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "City of Philadelphia", "Loudoun County Public Schools",
        "Boys & Girls Clubs of America",

        "US Department of Veterans Affairs", "Fairfax County Government",
        "District of Columbia Public Schools", "Greenville County Schools",
        "Houston Independent School District", "KIPP Texas Public Schools",
        "North Central Texas Council of Governments", "Texas Water Development Board",
        "State of South Dakota", "The City of lake city", "Governmentjobs",
        "Arizona Public Service (APS)", "Wisconsin", "Little Scholars of Arkansas",
        "St. Patrick's School Yorktown", "Future Promise Educational Services",
        "Center for Employment Opportunities", "Hana Center",
        "The Crime Victims Center/Parents for Megan's Law",
        "St. Vincent de Paul Society of Lane County", "RennerVation Foundation",
        "CALSTART", "Halvik", "Empower AI Inc.", "Torch Technologies",
    ],
    "Aerospace, Defense & Industrial": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Henkel", "Crown Equipment", "Valmont Industries", "Covestro",
        "Velo3D", "DXP Enterprises", "Woodward, Inc.", "Air Liquide",
        "Vast", "Pickle Robot Company",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Advanced Space", "Acron Aviation", "Stratasys", "Innomotics", "Amphenol", "Mueller Industries",
        "Klein Tools", "TGW Systems", "Ceco Environmental", "Messer", "Urban Sky", "Sofar Ocean",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "General Atomics",
        "Saronic Technologies", "JELD-WEN", "Carbon", "GRVTY",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Sabre Systems", "ENSCO, Inc.", "Rapiscan Systems", "Barbaricum", "Arcfield",
        "Leonardo DRS", "SimVentions", "Mach Industries", "Merlin Labs", "Muon Space",
        "Ursa Major", "HavocAI", "CHAOS Industries", "Helion", "E-Space", "Hubble Network",
        "Quindar", "Virgin Galactic", "Janicki", "Panthalassa", "Neurophos", "Radiant",
        "Path Robotics", "Carbon Robotics", "Orchard Robotics", "Lumafield", "Bombardier",
        "Pentair", "Lincoln Electric", "Swagelok", "NIBCO INC.", "Greenheck Group",
        "SPX Technologies", "Ball Corporation", "Constellium", "MAHLE", "OPmobility",
        "Amerequip", "King Technologies, Inc.", "Greiner", "ANODIZE", "Kelso Industries",
        "Springs Window Fashions", "Sub-Zero Group, Inc", "GE Appliances", "TK Elevator",
        "ZEISS Group", "SICK", "Allegion", "Buckman", "H.B. Fuller", "Hexion Inc.", "Eastman",
        "Milliken & Company", "Carhartt", "MicroVision", "Matthews",

        "Clarios", "Belden", "Trillium Flow Technologies", "Amentum", "James Hardie",
        "Merrick & Company", "Quanta Services", "Gentherm", "TechnipFMC", "Konecranes",
        "Primetals Technologies", "Generac Power Systems", "Southwire Company",
        "CEMEX", "Mitsubishi Power Americas, Inc.", "Trane Technologies",
        "Mueller Water Products", "Muellerwaterproducts", "Maxcess International",
        "Chamberlain Group", "TMEIC", "Tarkett", "BEUMER Group", "Arkema",
        "Environmental Resources Management", "Bekaert", "Advanced Composites",
        "Zekelman Industries", "Westinghouse Electric Company, LLC", "Core & Main",
        "SAF-HOLLAND", "Vixxo", "BRP", "Consolidated Precision Products", "Copeland",
        "Excelitas Technologies", "Heidelberg Materials", "Oetiker", "RHI Magnesita",
        "VOTAW PRECISION TECHNOLOGIES", "Yancey Bros CO.", "A123 Systems", "Rogers",
        "thyssenkrupp", "Holtec International", "CHEP", "Hendrickson USA LLC",
        "Firmenich", "PrimeSource Building Products", "Terex Corporation", "Knapp",
        "KONE", "Zeeco", "Oregon Tool", "Innophos", "allnex", "Barry Callebaut",
        "Braun Intertec", "Maesa", "Resideo", "ALSTOM", "CAE", "Canon", "Keyence",
        "Williams International", "Big Dutchman Inc.", "Advanced Nutrients",
        "Seohan-NTN Driveshaft", "Traton R&D", "Sicpa", "Lallemand Bio Ingredients USA LLC",
        "Kairos Power", "Pacific Fusion", "Heirloom Carbon Technologies, Inc.",
        "Nexamp", "Silicon Ranch", "Avantus", "Cape Electrical Supply LLC.",
        "ABC Supply Co., Inc.", "Winsupply", "Srsdistribution", "QXO", "Sunbelt Rentals",
        "POWER ELECTRONICS", "Alliance Fire Protection", "Engineered Systems, Inc.",
        "LaForce Inc", "Shuttleworth LLC", "Rocket EMS", "Nova Credit",
            "Atomic Machines", "Rhombus Power", "Heidelberg",
    ],
    "Energy & Utilities": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Wood Mackenzie", "Plug Power Inc", "Triumvirate Environmental", "RigUp",
        "General Matter",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Arcadia",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "Landis+Gyr",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Enviva", "Par Pacific Holdings", "HF Sinclair", "bp", "Enbridge", "RWE",
        "Vistra Corp.", "Wabash Valley Power Alliance", "Clean Harbors", "WM",
        "Mariana Minerals", "CMC",

        "NiSource US", "FirstEnergy Corp.", "Pacific Gas and Electric",
        "Southern California Edison Company", "Spire", "Avangrid", "Vitol",
        "Kinetic Inc", "Gridware", "EdgeConneX", "Renewed Vision",
    ],
    "Engineering, Construction & Real Estate": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Roofstock", "SOCOTEC", "RE/SPEC Inc", "SitelogIQ",
        "J.F. Electric",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "Oldcastle BuildingEnvelope", "Mesa Associates, Inc.", "Qualus", "Castle Rock Associates", "Boccard",
        "Johns Manville", "Haworth", "Graybar",
        "Amrize", "Rimkus", "Hatch",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "CDM Smith", "Woodard & Curran", "Harris & Associates", "Woolpert", "Wade Trim",
        "Swinerton", "Brasfield & Gorrie", "MasTec Inc", "GeoStabilization International",
        "Gene B. Glick Company", "PERI", "Reliance, Inc.", "CRANSTON",

        # added 2026-08-24, see the note above
        "Newmark", "Mott MacDonald",
        "HDR", "Allan Myers", "Luster National", "CannonDesign", "Core Spaces",
        "Lithko Contracting", "Bolton & Menk", "Mead & Hunt, Inc.", "Inframark",
        "Olsson", "Forgen", "Hypower Inc.", "LandDesign, Inc", "Ballinger",
        "Crown Field Services", "Day & Zimmermann", "Lennar", "Lemartec",
        "McLemore Building Maintenance", "Resilient Retrofits", "Restoration Relief",
        "Restoration East LLC", "Taylor Fence Company", "Mike Home improvements",
        "Jones Mobile Home Service INC", "Johnny on the Spot Environmental",
        "Environmental Science Associates", "FirstService Residential", "Bozzuto",
        "Industrious", "Cortland", "Arhaus", "Mantis Innovation", "Avicado",
        "Luxury Presence", "Serhant", "Core Spaces", "Gmh", "LotusWorks",
        "Qualdoc", "Abebe Westside LLC", "Landmere, Inc.", "Rockland Express LLC",
        "GardaWorld Security Services US", "Johnson Brothers", "Ignite Fueling Innovation",
        "Good Life Corporation", "Solari, Inc.",
            "CUPERTINO ELECTRIC",
    ],
    "Transport, Logistics & Automotive": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Radial", "ArcBest", "Fleetpride", "United States Cold Storage",
        "Drivemode",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "TrueCar, Inc.",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Herc Rentals", "Iron Mountain", "Flexjet", "Ohio Cat", "HAVI", "42dot", "Uniti",
        "BusPatrol", "Falcor Express LLC", "ABM Industries Inc.", "Continental",

        "RXO", "C.H. Robinson", "R+L Carriers", "AutoNation", "Syncreon", "Gotion",
        "AeroVect", "Avride", "WeRide", "Keolis", "Swissport", "BYD America",
        "Contemporary Amperex Technology Kentucky LLC", "CarMax", "Vivint",
        "Array Technologies", "Autotech", "SkyRyse", "Supernal", "Vay", "Wayve",
        "Dexmate", "RoboForce", "Paradigm Van", "Town Pump", "QuikTrip",
        "Wolverine Worldwide", "Spreetail", "Gopuff", "ACV Auctions", "Motion",
        "Iko", "Everpure",
    ],
    "Retail, Consumer & Hospitality": [
        # + 2026-09-02: the adoption run added 269 employers to the universe and took
        # Unsorted from 1.4% to 6.7%, over the ceiling. Ambiguous names resolved from
        # the BOARD URL, never the name; genuinely unrecognisable ones left Unsorted.
        "Express", "Rust-Oleum", "Fairlife, LLC", "McCormick & Company",
        "Brown-Forman", "Amway", "Patagonia", "iFIT",
        "Amplifon",
        # + adopted 2026-08-31, ranked-sponsor probe batch 2 (federal spellings).
        "BEYOND INC", "ULTA INC", "WEEE INC",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "24 Hour Fitness", "Guitar Center", "Inspirato", "Servpro", "HP Hood", "Atoms",
        # + 2026-08-31 refresh, newly prominent -- see the note on _CURATED_TAIL.
        "Fanatics", "Tapestry", "Sonos, Inc.", "David",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "HSN", "Misfits Market", "BABYLIST", "goodr", "The Farmer's Dog", "Brooks Running",
        "Garage Clothing", "Claire's", "Runnings", "Petco", "Sweetwater", "Sol de Janeiro",
        "Primo Brands", "Simplot Company", "Gruma", "ITG Brands", "GALLO", "Johnsonville",
        "Bruegger's Bagels", "Five Guys", "Eataly", "Flamingo", "HEB", "Staybridge Suites",
        "gate group", "Varsity Brands", "1-800-GOT-JUNK?", "Paul Davis Restoration",
        "COVERCRAFT INDUSTRIES", "Almo Corporation", "Border States", "BlueLinx", "Veritiv",
        "Cencora", "PartsSource Inc.", "Sunrise Senior Living", "L'Oreal", "L'Oréal",  # both spellings on purpose: norm_company maps the accented form to a DIFFERENT key
        

        # added 2026-08-24, see the note above
        "Carvana", "Whirlpool Corporation",
        "Crocs", "Crate and Barrel", "Best Western", "Five Below", "Hasbro, Inc.",
        "Puig", "Weis Markets", "Warby Parker", "New Balance", "The RealReal",
        "Richemont", "Rollins", "Golden State", "Gap Inc.", "Saks Global",
        "Columbia Sportswear", "National Vision, Inc.", "La-Z-Boy", "Thrive Market",
        "Build-A-Bear Workshop", "Chick-fil-A", "HelloFresh", "Sweetgreen",
        "Lamb Weston", "Giant Eagle", "Wawa", "Aramark", "Balsam Brands",
        "Shop LC", "Perry Ellis International", "Hot Topic", "Rent The Runway",
        "Burlington", "Dillards", "Chowbus", "Bombas", "Faherty Brand",
        "Hill House Home", "Fashion Nova", "Blank Street", "KISS Products",
        "Chefman", "CookUnity", "Winebow", "Central Coast Wine Company",
        "Hilmar Cheese Company", "The Morning Star Company", "Big Geyser, Inc",
        "Ferrero", "Kerry", "OFI", "Sodexo", "Crunch Fitness", "ResortPass",
        "GetYourGuide", "Twin Peaks Inc", "BONITA BAY CLUB", "Carl Fischer LLC",
        "Hattori Hanzo Shears, Inc.", "Drim Commerce LLC", "RepRally", "WHOP INC",
        "Ernest", "Solera", "Maesa", "Sleep Number Corporation", "Arhaus",
        "Prologis", "Insperity",
            "The SSA Group",
    ],
    "Media, Telecom & Gaming": [
        # + adopted 2026-08-31 from the ranked-sponsor probe.
        "ROKU INC",
        # + 2026-08-31 discovery-sweep arrivals, curated by board URL.
        "2K",
        # discovery-sweep tail, curated 2026-08-24 -- see the note above
        "Consumer Reports", "AXS", "Bisnow", "Twitch", "Red Ventures", "Wiley", "Level99",
        "DAS North America", "Antares", "Solstice",

        # added 2026-08-24, see the note above
        "Ericsson", "Orchestra",
        "Nexstar", "Telus", "SiriusXM", "Genius Sports", "Scopely", "Xsolla",
        "Crunchyroll", "AccuWeather", "Real Chemistry", "Known", "VaynerMedia",
        "Taboola", "Samba TV", "Optimum", "SEMAFOR", "Relx", "Informa Markets Medica LLC",
        "MobilityWare", "TapBlaze", "Carl Fischer LLC", "LeagueApps Inc.",
        "Jack Morton Worldwide", "Brainlabs", "PMG", "FleishmanHillard",
        "Havas Pr North America INC", "Code and Theory", "VML", "Dentons",
        "RR Donnelley", "Nexstar",
    ],
}


# Third pass, and the reason there is one: the first two were built from the LOCAL SNAPSHOT and
# a boards table read out of .streamlit/secrets.toml -- the retired Streamlit app's credentials,
# still pointing at the Supabase this project moved off. That is 21,980 rows and 167 boards.
# Live is 27,613 rows and 115 boards, which is 591 employers the earlier passes never saw.
# _corpus_source() now prints which database it read, so this cannot happen quietly again.
#
# The nine at the top are the boards whose stored name was a tenant slug until 2026-08-22
# (Hdpc -> Goldman Sachs and friends). They needed sectors of their own, because until now no
# rule had ever seen their real names.
_CURATED_LIVE = {
    "Aerospace, Defense & Industrial": [
        "Fortive", "Abb", "ABB", "Assaabloy", "Schneider Electric", "Xylem", "Symbotic",
        "Vertiv", "Molex", "INNIO", "IMI Plc", "Plexus", "Acuity Brands", "Inteplast",
        "Mosaic", "Waters", "INFICON", "Dorman Products", "ClarkDietrich Building Systems",
        "Virginia Transformer Corp", "Ultralife Corporation", "XNRGY Climate Systems",
        "Polaris Industries", "Shark Ninja", "SCHNELLER INC", "Hamilton Company",
        "Solidigm US", "Ultra", "Neumo Holdings LLC", "INC Andersen Windows",
        "Agile Space Industries", "Astranis", "True Anomaly", "GhostEye", "Melius",
        "Long Wave Inc.", "KIHOMAC, Inc.", "North Point Technology", "Envisioneering, Inc",
        "WR Systems", "SimIS, Inc", "ManTech International", "Chenega Corporation",
        "Chenega MIOS", "Cherokee Federal", "CATHEXIS", "Nuvitek", "Northstrat",
        "Pantheon Data", "Patrona Corporation", "Belay Technologies", "Marathon TS Inc",
        "Cambridge International Systems, Inc", "TMC Technologies", "LMI",
        "HII's Mission Technologies division", "Genuine Parts Giant", "Lexicon, Inc.",
        "Apex Companies, LLC", "CSA Group", "Hitachi Rail", "CMA CGM", "Trillium",
        "Armada", "Corvant", "Novaflow", "Minicor", "Renova One", "RS Electric",
        "Humble Robotics", "Lambda Robotics", "Autonomous Technologies Group",
        "Peregrine Technologies", "QUANTUM TECHNOLOGIES LLC", "Terranox AI",
    ],
    "Banking, Finance & Insurance": [
        "Kroll", "Cantor Fitzgerald", "OnePay", "J.P. Morgan", "Fitch Ratings", "Broadridge",
        "Verisk", "Corpay", "FIS", "Forge Global", "Manulife", "ION Group", "Qbe",
        "HealthEquity Inc.", "Ibotta", "Remitly, Inc.", "Washington Trust", "Swyfft",
        "Tomo Credit", "Bestow", "Boldin", "Thunes", "Bullish", "Gemini", "MyFunded Futures",
        "Initio Capital", "LP Analyst", "Selene Diligence", "DiligenceSquared",
        "The Independent Community Bankers of America", "Texas Farm Bureau", "HCVT",
        "CohnReznick", "Cotiviti", "R1 RCM", "MeridianLink", "Ninth Wave", "PayIt",
        "Spade", "TaxHawk", "Elliptic", "Key To Web3", "Pulley", "Rho", "Confido",
        "Tabs", "Dealops", "MRI", "Brookfield Corp.", "HUB International", "Press Ganey Associates",
    ],
    "Healthcare, Pharma & Biotech": [
        "Pace Analytical", "Catalent", "Tempus", "Personalis", "GoodRx", "CenterWell",
        "Tandem Diabetes Care, Inc.", "Alphatec Spine", "Stereotaxis", "Verathon",
        "Intuitive Surgical, Inc", "ThermoFisher Scientific", "LabConnect",
        "Clinical Reference Laboratory", "VivoSense, Inc.", "Abby Care", "Herewith",
        "OCHIN, Inc.", "HCSC", "Metriport", "Alma", "Papa", "Unlearn", "Edison Scientific",
        "DigiM Solution LLC", "Amorepacific Us", "Advocates", "PRC Baker Places",
        "ColumbiaCare Services", "IHMS LLC.", "Somatus", "American Heart Association",
    ],
    "Hospitals & Health Systems": [
        "UC Health", "KAISER", "Seattlechildrens", "Minerva",
    ],
    "Energy & Utilities": [
        "World Fuel Services", "Evergy", "ONE Gas", "Venture Global LNG", "Enverus",
        "Lynker", "PROtect", "VSC Fire", "Hermanson Company", "USIC", "Pkaza",
    ],
    "Software & Internet": [
        "Gartner", "Proofpoint", "PaloAlto Networks", "Arctic Wolf", "Genesys", "UKG",
        "LiveRamp", "Mixpanel", "Bullhorn", "Blueprint", "OneSpan", "Synaptics",
        "SquareTrade", "PAR Technology", "Wolters Kluwer", "EBSCO Information Services",
        "EverCommerce", "Encord", "Midjourney", "Superhuman", "Magical", "Medium",
        "Materialize", "Fountain", "Flip", "Awardco", "Hex Technologies", "TRACKVIA INC",
        "Kofile Technologies", "N. Harris Computer Corporation - USA", "Railinc Corp.",
        "Gainwell Technologies LLC", "PROLIM Corporation", "Boston Technology Corporation",
        "Nucleus Security", "Hinoki Security", "Pi Security", "Secureframe", "HR Acuity LLC",
        "Kiddom", "Stride, Inc.", "NORY", "Kwik Trip Inc", "Hive", "Loop", "Pocket",
        "Commence", "Chalk", "Dealpath", "Casechek", "CourtAlert", "GovWell", "Parkade",
        "Pantograph", "Translucent", "Trovy", "Soren", "Sixtyfour", "Twenty", "Thesis*",
        "Pronto", "Naïve", "tonic", "Mirage", "Expression", "Allocate", "Affinity",
        "Artisan", "Circleback", "Conduit", "Cultura", "Finny", "foundr", "Freebuff",
        "Giftogram", "GloGlo", "Human Archive", "Lance", "Lean Layer", "Link Network",
        "Manifest OS", "Nectar Social", "Pensive", "PEX+", "Quadrillion", "ReadyOn",
        "Relace", "Runbook", "SkyLink", "Sporting Kansas City", "TeamOut", "Valkai",
        "Veho", "ZipLine", "ornn.com", "joinanvil.com", "Employer.com", "Pilot.com, Inc.",
        "Future Dial, Inc.", "Likewize", "Zensors", "SpreeAI", "Instalily.Ai", "Hatz AI",
        "Haize Labs", "Bespoke Labs", "Dedalus Labs", "David AI", "ArtosAI", "Billee.AI",
        "Rhizome AI", "Soulside AI", "DeepAware AI (Robotics Center of Silicon Valley)",
        "Openkyber", "Edyo", "Ivo", "dili", "Clera", "Fort", "Vibrant Planet", "LVT",
        "KASTLE", "LIGHTFEATHER IO LLC", "Atominvest", "Canopy Works", "CharacterQuilt",
        "Sunland Group, Inc.", "Datalab", "Ooak Data", "Minneapolis Public Schools",
        "Merge API Integration Sandbox", "Acme 091614", "TAb S", "Trove Brands",
        "Vizio Services", "Bitdeer Technologies Group", "Bet365", "MetroStar",
        "NuAxis Innovations", "GovCIO", "PSI Services", "CAI", "C1", "ARGO",
        # fourth pass: the last few the live sweep left that a name can actually settle
        "Mistral", "Tagup", "Lynx", "GreenArrow", "SERVAL SAS",
        "Copart", "Grubhub", "Hearst", "News Corp", "New York Post", "The Arena",
        "Concord USA", "ConvergeOne", "Blueprint", "Fab2", "Pbv", "Impact",
    ],
    "IT Services & Consulting": [
        "SynergisticIT", "AaraTechnologies Inc", "Eliassen Group", "Mindlance",
        "Turner & Townsend", "Pinnacle Technical Resources", "Stefanini", "WinWire",
        "Sharp Decisions", "nLeague Services", "Sparks Group", "Cognitive Minds LLC",
        "Incedo Inc.", "AccrueTalent", "Brooksource", "Aldridge", "America At Work",
        "Cadmus", "Omega Technical Services", "Fast Switch", "Conch Technologies Inc",
        "Donato Technologies, Inc", "Staxa Technologies", "Allegis Group",
        "Comprehensive Resources Inc.", "Select Minds LLC", "Resource Informatics Group",
        "Infojini Inc", "Skywalk Global", "TechniPros, LLC", "Hays", "Inteliblue",
        "ExcelGens, Inc.", "Nextgen Information Services", "Info Origin Inc.",
        "Redolent, Inc", "Spotlight Inc.", "Tekaccel, Inc", "Link Technologies",
        "Aston Carter", "TransPerfect", "Merican Inc", "IT Labs", "Page Group",
        "Jobot", "Genesis10", "Global Channel Management, Inc.", "Hired by Matrix",
        "HumCap, Inc.", "Bishop & Company, Inc.", "The Bachrach Group", "Russell Tobin",
        "Tailored Management", "TASC Technical Services", "Staffingine LLC", "Vitaver",
        "Varick Agents", "Vibesoft Inc", "Uhler & Company", "Smith Johnson Tech",
        "Silicon Valley Search Group", "Smart Synergies", "RimePro Inc", "Qureos Inc",
        "PDSSOFT INC.", "Parkar Global Technologies Pvt. Ltd.", "Ohm Systems, Inc",
        "NasTech Global, Inc.", "MW Partner", "MRINetwork Jobs", "Magnet Hr", "LHH US",
        "Medinext Global LLC", "Neeljym Search Group", "North Shore Strategies",
        "Inter-co division 10 inc", "Info Dinamica Inc", "HigherPeople", "HME Careers",
        "Harris & Co Executive Search", "Gottlieb and Greenspan", "GARGI TECHNOLOGIES INC",
        "ERSG Ltd", "Express Employment Professionals", "Adidev Technologies Inc",
        "Adaptive Innovations", "AEM Corporation", "AllSTEM Connections", "AppleOne",
        "A & Associates", "347 Group, Inc.", "1STAR-NETWORKS LLC", "Black Rock Groups",
        "Brandes Associates", "Cinter Career", "Element 6solution", "RZR Global",
        "STAND 8", "Tri-City Group", "Tresume and Asta CRS", "Jose Merciline",
        "Relling", "Menlo", "Minicor", "MW Partner", "Cooley LLP",
        "Cooley Godward Kronish LLP", "PMAT", "The Project Delivery Group", "Plexos Group, LLC",
        "OOS Management", "Bnaus Bbdo Usa", "McCann Relationship Mktg", "SBS Creatix, LLC",
        "Boston Technology Corporation", "STAMPEDE VENTURES INC", "Selene Diligence",
    ],
    "Engineering, Construction & Real Estate": [
        "Kleinfelder", "Mortenson", "Menard", "Menards", "TERRACON", "Groundworks",
        "IPS-Integrated Project Services", "Sargent & Lundy", "Hoefer Welker",
        "Shiel Sexton Company, Inc.", "Fisher Associates, P.E., L.S., L.A., D.P.C.",
        "GeoSurfaces", "ADB Companies Inc", "BMS CAT", "DCS Asset Maintenance",
        "Greystar Management Services", "K&D Development", "Lithko", "The Chamberlain Group",
        "Ford Audio-Video Systems", "A. Duda", "American Made Signs", "Breaking Ground",
        "Tri-Coastal", "FBS MANAGEMENT LLC", "Sorrel River Ranch", "Parkade",
    ],
    "Retail, Consumer & Hospitality": [
        "7-Eleven", "Gapinc", "Homedepot", "CHS Inc.", "Ardent Mills", "The Wonderful Company",
        "Fabletics", "HUGO BOSS", "Pair Eyewear", "Indie Campers", "Lindblad Expeditions",
        "Big Geyser, Inc", "Giftogram", "Sorrel River Ranch", "Trove Brands",
    ],
    "Transport, Logistics & Automotive": [
        "Corpay", "USIC", "Veho", "CMA CGM", "Metropolitan Transportation Authority",
    ],
    "Media, Telecom & Gaming": [
        "Hearst", "News Corp", "New York Post", "McCann Relationship Mktg", "Bnaus Bbdo Usa",
        "Indie Campers", "Minnetrista Museum & Gardens", "Indiana Sports Corp",
    ],
    "Government & Public Sector": [
        "City of New York", "State of careers Rhode Island", "ELIZABETH PUBLIC SCHOOLS",
        "Natick Public Schools", "Minneapolis Public Schools", "Developing NYS",
        "Metropolitan Transportation Authority", "Open Technology Fund",
        "International Rescue Committee", "International Justice Mission",
        "Catholic Social Services", "FRIENDS OF THE LIBRARY OF HAWAII",
        "The Church of Jesus Christ of Latter-day Saints", "Minnetrista Museum & Gardens",
        "CPR COURSES INTERNATIONAL LLC", "NPAA",
    ],
}
for _sector, _names in _CURATED_LIVE.items():
    _CURATED_TAIL.setdefault(_sector, []).extend(_names)

CURATED = {core.norm_company(n): s for s, names in _CURATED_LISTS.items() for n in names}
# The tail loses to the corrections block on a collision: _CURATED_LISTS was written against
# measured traffic, the sweep was written from a name.
for _sector, _names in _CURATED_TAIL.items():
    for _n in _names:
        CURATED.setdefault(core.norm_company(_n), _sector)


def _squash(s):
    """'Palo Alto Networks' -> 'paloaltonetworks'. Same idea as probe_migratemate._squash."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# A SECOND index on the space-free form, because the corpus spells the same employer several
# ways and a space is the difference between a hit and a miss. Live data carries "PaloAlto
# Networks", "ThermoFisher Scientific", "Homedepot", "Gapinc", "Assaabloy" and
# "Seattlechildrens" -- every one of which is a company already named in the lists above, and
# every one of which was landing in Unsorted. Matching the squashed form costs one dict lookup
# and removes a whole class of near-miss.
CURATED_SQUASHED = {}
for _sector, _names in list(_CURATED_LISTS.items()) + list(_CURATED_TAIL.items()):
    for _n in _names:
        CURATED_SQUASHED.setdefault(_squash(_n), _sector)


def _sector(name, key, cap_exempt, agency):
    """(sector, which_layer_won). Priority: curated > shipped rules > keywords > Unsorted."""
    hit = CURATED.get(key)
    if hit:
        return hit, "curated"
    hit = CURATED_SQUASHED.get(_squash(name))
    if hit:
        return hit, "squashed"
    # The two shipped helpers beat keywords because they are already gated by tests and are
    # already what the .cx / .agency badges on the page mean.
    if cap_exempt:
        return (("Hospitals & Health Systems", "rule") if _HOSPITAL_RE.search(name)
                else ("Universities & Research", "rule"))
    if agency:
        # A body shop IS an IT services firm. The .agency flag is what distinguishes it, not a
        # section of its own -- nobody browses "Staffing" looking for a job.
        return "IT Services & Consulting", "rule"
    for sector, rx in _KEYWORDS:
        if rx.search(name):
            return sector, "keyword"
    return UNSORTED, "unsorted"


# --------------------------------------------------------------- inputs
def _read_sponsors(path="sponsors.txt"):
    """Display names from sponsors.txt, ignoring comments and the section markers."""
    out = []
    if not os.path.exists(path):
        return out
    for raw in open(path, encoding="utf-8"):
        s = raw.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


_CAREERS = re.compile(r"^-\s+(.+?)\s+·.*?\[Careers page\]\((https?://[^)]+)\)", re.M)


def _read_careers_md(path="careers_us.md"):
    """{display_name: careers_url} for the entries carrying a [Careers page] link.

    careers_us.md stays the place a careers URL is EDITED by hand -- build_careers_md.py
    round-trips it -- so it is read here rather than duplicated.
    """
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    return {m.group(1).strip(): m.group(2).strip() for m in _CAREERS.finditer(txt)}


def _corpus_source():
    """('db'|'snapshot', explanation). Which corpus this build should read, and why.

    THE DATABASE reads first when a proxy or a direct DSN is configured, and this order is the
    whole point. The first build of companies.json silently used the local snapshot (21,980
    rows) and a boards table read from .streamlit/secrets.toml -- credentials left behind by the
    retired Streamlit app, pointing at the Supabase this project moved OFF on 2026-08-15. Live
    was 27,613 rows and 115 boards. Nothing failed; the file was just built from a database
    nobody reads any more, and said so nowhere.
    """
    if os.environ.get("PG_DSN"):
        return "db", db.backend_name()
    if os.environ.get("DB_PROXY_URL") and os.environ.get("DB_PROXY_SECRET"):
        return "db", db.backend_name()
    if os.path.exists(SNAPSHOT):
        return "snapshot", ("%s -- NO proxy configured, so this is whatever the last run left "
                            "behind" % SNAPSHOT)
    return "db", db.backend_name()


def _corpus_counts():
    """({norm_key: open_count}, {norm_key: most_common_corpus_spelling}).

    A NARROW select either way: a bare db.load_jobs() downloads ~130 MB of descriptions to read
    one short string per row.
    """
    which, why = _corpus_source()
    rows = None
    if which == "db":
        try:
            rows = db.load_jobs(cols=db.COLS_COMPANY)
            sys.stderr.write("corpus: %d rows from %s\n" % (len(rows or []), why))
        except Exception as exc:
            sys.stderr.write("note: database unreachable (%s); falling back to %s\n"
                             % (type(exc).__name__, SNAPSHOT))
    if rows is None and os.path.exists(SNAPSHOT):
        try:
            blob = json.load(gzip.open(SNAPSHOT, "rt", encoding="utf-8"))
            rows = blob.get("rows") if isinstance(blob, dict) else blob
            sys.stderr.write("corpus: %d rows from %s\n" % (len(rows or []), why))
        except Exception as exc:
            sys.stderr.write("note: %s unreadable (%s)\n" % (SNAPSHOT, type(exc).__name__))
    if rows is None:
        sys.stderr.write("note: no corpus at all; live counts will be 0 and corpus-only "
                         "companies will be missing\n")
        return {}, {}
    counts = collections.Counter()
    spellings = collections.defaultdict(collections.Counter)
    for r in rows or []:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        key = core.norm_company(name)
        # `is not False` and not `not ...`: is_active is None on an un-migrated row, and only
        # an explicit False means "we checked and the posting is gone". Same rule as web.py.
        if r.get("is_active") is not False:
            counts[key] += 1
        spellings[key][name] += 1
    best = {k: c.most_common(1)[0][0] for k, c in spellings.items()}
    return counts, best


def _universe():
    """({norm_key: display_name}, {norm_key: board_url}) over every company we scrape or
    know sponsors. Earlier sources win the display name, so SOURCES -- what the scraper calls
    it -- beats sponsors.txt.
    """
    uni, boards = {}, {}

    def add(name):
        name = (name or "").strip()
        if not name:
            return ""
        key = core.norm_company(name)
        if key and key not in uni:
            uni[key] = name
        return key

    def _browsable(u):
        """A careers link has to be something a person can open. The live boards table holds 13
        rows whose url is a sentinel rather than an address -- 'adzuna:ADP', left behind when
        that source was dropped on 2026-08-16 -- and they are inert to the scraper (their
        ats_type is not in SCRAPERS) but would otherwise have become 13 unclickable "Careers"
        buttons. test_companies_page catches this class; it is why that assertion exists."""
        return (u or "").startswith(("http://", "https://"))

    for url, _ats, name in scraper.SOURCES:
        key = add(name)
        if key and _browsable(url):
            boards.setdefault(key, url)
    try:
        rows = db.list_boards() or []
        sys.stderr.write("boards: %d rows from %s\n" % (len(rows), db.backend_name()))
        for b in rows:
            key = add(b.get("company"))
            url = (b.get("url") or "").strip()
            if key and _browsable(url):
                boards.setdefault(key, url)
    except Exception as exc:
        sys.stderr.write("note: boards table unavailable (%s); companies added through /add "
                         "will be missing from this build\n" % type(exc).__name__)
    for name in _read_sponsors():
        add(name)
    return uni, boards


# --------------------------------------------------------------- build
def _encode(url):
    """Shorten a board URL against PREFIX. The client expands "gh|samsara" back out."""
    for tag, pre in PREFIX.items():
        if url.startswith(pre):
            return "%s|%s" % (tag, url[len(pre):])
    return url


def _visa_index():
    """visa_tags.json as {norm_key: bitmask}, minus its provenance header."""
    try:
        blob = json.load(open("visa_tags.json", encoding="utf-8")) or {}
    except Exception:
        return {}
    blob.pop("#meta", None)
    return blob


def _previous_members(path=OUT_JSON):
    """{norm_key: name} from the companies.json already on disk, or {} if absent."""
    try:
        rows = (json.load(open(path, encoding="utf-8")) or {}).get("rows") or []
    except Exception:
        return {}
    out = {}
    for r in rows:
        name = (r[0] if isinstance(r, list) and r else "") or ""
        key = core.norm_company(name)
        if key:
            out.setdefault(key, name)
    return out


def build(carry=True):
    uni, boards = _universe()
    counts, spellings = _corpus_counts()

    # Corpus-only companies: in the feed, absent from every registry. Omitting these would
    # hide employers with live jobs, which is the worst bug this page could have.
    for key, name in spellings.items():
        uni.setdefault(key, name)

    # CARRY FORWARD everything the previous build knew about. Membership otherwise tracks
    # LIVE postings -- _universe() is SOURCES + boards + sponsors.txt, plus corpus spellings
    # above -- so an employer whose postings all expired and who has no board simply vanishes
    # from the directory. Measured across six days in Aug 2026: 470 companies dropped and 182
    # arrived, and the 468 that were truly gone included Sony, Zoom, Nutanix, Citadel
    # Securities, Wells Fargo Bank and Capgemini America -- all real employers who simply had
    # nothing open that week. A directory that forgets them is worse than one that carries a
    # few stale rows: the H-1B history and careers link are still true, and the live count is
    # computed per request (web.py::_company_stats), so a quiet employer honestly reads 0.
    #
    # setdefault, so a fresh spelling still wins over the one already on disk.
    carried = 0
    if carry:
        for key, name in _previous_members().items():
            if key not in uni:
                uni[key] = name
                carried += 1
        if carried:
            print("carried forward %d company/companies with no live postings and no board"
                  % carried)

    native_by_key = {core.norm_company(n): u for n, u in NATIVE.items()}
    md_by_key = {core.norm_company(n): u for n, u in _read_careers_md().items()}
    domains = {}
    try:
        blob = json.load(open("company_domains.json", encoding="utf-8"))
        domains = {core.norm_company(k): v for k, v in (blob.get("domains") or {}).items()}
    except Exception:
        pass
    sponsor_counts = core.load_sponsor_counts()
    visa = _visa_index()

    rows, report, hist, unsorted_rows = [], [], collections.Counter(), []
    for key, name in sorted(uni.items(), key=lambda kv: kv[1].lower()):
        cap = core.is_cap_exempt(name)
        agency = core.is_agency(name)
        sector, src = _sector(name, key, cap, agency)
        hist[sector] += 1

        # Careers ladder, first hit wins. NATIVE outranks the board URL deliberately: for
        # Amazon it is amazon.jobs pre-filtered to the US, and ~50 NATIVE names have no board.
        careers = native_by_key.get(key) or md_by_key.get(key)
        kind = KIND_NATIVE
        if not careers:
            careers, kind = boards.get(key), KIND_BOARD
        if not careers and domains.get(key):
            # Root only. The domain map was built by probing an ICON, so any /careers path we
            # appended would be a guess -- and web.py::logodomain already documents that
            # guessing <name>.com yields northwestern.com and flagstarbank.com, each a 404.
            careers, kind = "https://%s" % domains[key], KIND_SITE
        if not careers:
            kind = KIND_NONE

        mask = int(visa.get(key) or 0)
        if cap:
            mask |= BIT_CAP_EXEMPT
        if agency:
            mask |= BIT_AGENCY
        h1b = int(sponsor_counts.get(key) or 0)
        live = int(counts.get(key) or 0)

        # NEITHER the live count NOR the corpus spelling is stored. The scrape runs 4x a day
        # and this script runs by hand, so a baked count would be stale within hours; and the
        # spelling is what /company?c= must carry (db.block_key does not strip legal suffixes,
        # so "Accenture" finds nothing where the corpus says "Accenture LLP"). web.py resolves
        # both per request from the corpus it already holds in memory -- see _company_stats.
        rows.append([name,
                     SECTORS.index(sector) if sector in SECTORS else -1,
                     _encode(careers) if careers else "", kind,
                     domains.get(key) or "", h1b, mask])
        report.append({"name": name, "sector": sector, "src": src, "careers_kind": kind,
                       "live_jobs": live, "h1b": h1b,
                       "cap_exempt": int(cap), "agency": int(agency)})
        if sector == UNSORTED:
            unsorted_rows.append((live, h1b, name))

    blob = {
        "built_at": datetime.date.today().isoformat(),
        "sectors": SECTORS,
        "prefix": PREFIX,
        "li_kw": LI_KEYWORD,
        "rows": rows,
        "note": "Built by scripts/build_companies.py; sector provenance in " + OUT_CSV,
    }
    return blob, report, hist, unsorted_rows


def _prominent(report, top=400):
    """The companies --check refuses to leave Unsorted: the head by live postings plus every
    high/medium-volume sponsor. 88% of postings and 89% of filings sit in that head, which is
    the part a curated map actually has to cover."""
    ranked = sorted(report, key=lambda r: -r["live_jobs"])[:top]
    out = {r["name"] for r in ranked if r["live_jobs"] > 0}
    for r in report:
        # sponsor_strength tiers on volume: >=1000 high, >=100 medium.
        if r["h1b"] >= 100:
            out.add(r["name"])
    return out


def main(argv):
    want_report = "--report" in argv
    want_check = "--check" in argv
    # --no-carry rebuilds membership from the live universe only, dropping employers with
    # no postings and no board. That is the pre-2026-08-31 behaviour; use it to prune.
    blob, report, hist, unsorted_rows = build(carry="--no-carry" not in argv)
    total = len(report)

    if want_report or want_check:
        print("%d companies" % total)
        for sector in SECTORS + [UNSORTED]:
            n = hist.get(sector, 0)
            print("  %-42s %5d  %4.1f%%" % (sector, n, 100.0 * n / max(total, 1)))
        by_src = collections.Counter(r["src"] for r in report)
        print("  layers: " + ", ".join("%s=%d" % kv for kv in by_src.most_common()))
        print("  careers link: %d native/board, %d site-root, %d LinkedIn only"
              % (sum(1 for r in report if r["careers_kind"] in (KIND_NATIVE, KIND_BOARD)),
                 sum(1 for r in report if r["careers_kind"] == KIND_SITE),
                 sum(1 for r in report if r["careers_kind"] == KIND_NONE)))
        print("  with live jobs: %d" % sum(1 for r in report if r["live_jobs"] > 0))

    if want_report:
        print("\nTop Unsorted by live postings (curate these first):")
        for live, h1b, name in sorted(unsorted_rows, reverse=True)[:40]:
            print("  %5d jobs  %7d H-1B  %s" % (live, h1b, name))
        return 0

    if want_check:
        prominent = _prominent(report)
        bad = sorted(r["name"] for r in report
                     if r["sector"] == UNSORTED and r["name"] in prominent)
        if bad:
            sys.stderr.write("\nFAIL: %d prominent companies are Unsorted. Add them to "
                             "CURATED or widen a keyword:\n" % len(bad))
            for n in bad[:60]:
                sys.stderr.write("  %s\n" % n)
            return 1
        # A ceiling as well as a floor. The per-company gate above cannot catch the failure
        # that actually happened once: the bucket sitting at 916 (43%) because the curated
        # lists had not been written yet, with every single name individually below the
        # prominence bar. 5% is deliberately loose -- a scrape can legitimately introduce a
        # batch of unfamiliar employers -- but it fails long before the page reads as broken.
        n_uns = hist.get(UNSORTED, 0)
        share = 100.0 * n_uns / max(total, 1)
        if share > UNSORTED_CEILING:
            sys.stderr.write("\nFAIL: Unsorted is %d of %d (%.1f%%), over the %.0f%% ceiling.\n"
                             "Run --report and add the head of that list to _CURATED_TAIL.\n"
                             % (n_uns, total, share, UNSORTED_CEILING))
            return 1
        print("\nOK: no prominent company is Unsorted, and the bucket is %d of %d (%.1f%%)."
              % (n_uns, total, share))
        return 0

    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "sector", "src", "careers_kind",
                                           "live_jobs", "h1b", "cap_exempt", "agency"])
        w.writeheader()
        w.writerows(report)
    print("Wrote %s (%d companies, %.0f KB) and %s"
          % (OUT_JSON, total, os.path.getsize(OUT_JSON) / 1024.0, OUT_CSV))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
