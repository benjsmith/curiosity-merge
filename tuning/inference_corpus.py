"""Labeled tuning corpus for the combined-data inference detector.

Each sample is a (text, expected_label) pair where expected_label is
True if the sample should fire a `gdpr_combined_inference` finding for
the given combination type, False otherwise.

Four combination types covered (matching v0.5.0 detector):
  PERSON_LOCATION_DATE  — full identification (PERSON + LOCATION + DATE_TIME)
  PERSON_ORG            — workplace identification (PERSON + ORGANIZATION)
  PERSON_AGE            — age-tied identification (PERSON + DATE_TIME-as-age)
  PERSON_MEDICAL        — professional identification (PERSON + MEDICAL_LICENSE)

Construction philosophy:
  - True positives: realistic prose where the entities co-occur in a
    single semantic unit. Variation in sentence structure, punctuation,
    paragraph context, presence of intervening words.
  - True negatives: prose where the entities appear but separated by
    significant text or topic shifts. Tests false-positive rate at
    larger windows.

Samples are short (single paragraph or sentence). The detector window
is char-distance-based, so testing at varied span sizes gives us a
clear signal for threshold selection.
"""
from __future__ import annotations


# --- PERSON + LOCATION + DATE_TIME (full identification) -----------------

PERSON_LOCATION_DATE = [
    # === True positives: classic identifying triples ==================
    ("Dr. Alice Johnson, born March 12 1985, lives in Boston.", True),
    ("Bob Smith was born in 1972 and resides in Berlin.", True),
    ("Carol Davis (b. 1990) is based in San Francisco.", True),
    ("David Lee, age 42 as of 2024, currently lives in Singapore.", True),
    ("Eve Martinez, born in 1968, has lived in Paris since 1990.", True),
    ("Frank O'Brien, born July 4 1976, resides in Dublin.", True),
    ("Grace Kim, born 1985 in Seoul, now lives in Tokyo.", True),
    ("Henry Wilson, born in 1955 in Manchester, retired to Brighton.", True),
    ("Iris Patel was born in Mumbai in 1992.", True),
    ("James Brown, born December 1 1980, is from Chicago.", True),
    ("Karen Lopez, b. February 1979, lives in Madrid.", True),
    ("Leo Anderson, born 1965, has been in Stockholm since 1990.", True),
    ("Maria Rossi, born September 1988, resides in Rome.", True),
    ("Nadia Khan, born 1991 in Lahore, lives in Toronto.", True),
    ("Oliver Schmidt, born 1973, has called Munich home since 1995.", True),
    ("Pamela Adams, born May 17 1962, lives in Sydney.", True),
    ("Quentin Mueller, born 1987 in Vienna, moved to Zurich in 2010.", True),
    ("Rachel Green, born 1969, currently lives in Auckland.", True),
    ("Sam Thompson, b. April 1983, resides in Edinburgh.", True),
    ("Tara Williams, born November 1975 in Cardiff, now lives in London.", True),
    ("Uri Cohen, born June 1981, has lived in Tel Aviv his whole life.", True),
    ("Vera Petrov, b. 1986, calls St. Petersburg home.", True),
    ("Walter Klein, born March 1968 in Hamburg, lives in Frankfurt.", True),
    ("Ximena Lopez, born 1990 in Mexico City, resides in Guadalajara.", True),
    ("Yusuf Ahmed, born 1977 in Cairo, has been in Dubai since 2005.", True),
    ("Zoe Bennett (born February 1984) currently lives in Wellington.", True),
    ("Alicia Reyes, age 35, lives in Barcelona since 2015.", True),
    ("Brian Foster, age 50 in 2024, resides in Vancouver.", True),
    ("Charles Hawking, age 67 in 2025, has lived in Cambridge since 1985.", True),
    ("Diana Pierce was born in 1979 in Portland.", True),
    ("Edgar Wallace, age 41, lives in Manchester.", True),
    ("Fiona Stewart, born 1984, has been in Glasgow since 2010.", True),
    ("Gerald Black, born 1958 in Liverpool, retired to Bath.", True),
    ("Helena Voss, b. 1992, lives in Copenhagen.", True),
    ("Ivan Petrov, born 1980, currently in Moscow.", True),
    ("Julia Schwartz, age 33 as of 2024, resides in Brussels.", True),
    ("Kevin Murphy, born March 1971, lives in Dublin's city centre.", True),
    ("Linda Carter, b. 1989, has been in Toronto for 10 years.", True),
    ("Michael Stone, born 1966 in Detroit, lives in Atlanta.", True),
    ("Natalie Wong, born 1988 in Hong Kong, resides in Singapore.", True),
    ("Omar Hassan, age 38, lives in Casablanca.", True),
    ("Patricia Bell, born 1975, has called Edinburgh home since 1990.", True),
    ("Quinn O'Hara, born 1993, lives in Belfast.", True),
    ("Ruth Allen, b. April 1980, currently in Auckland.", True),
    ("Steven Chang, born 1972 in Taipei, lives in Seattle.", True),
    ("Tina Roberts, age 45 in 2025, lives in Cape Town.", True),
    ("Ulrich Bach, born 1969 in Berlin, retired to Lugano.", True),
    ("Victoria Hayes, born December 1990, resides in Sydney.", True),
    ("William Dunn, age 60, has lived in Melbourne since 1980.", True),
    ("Xavier Cruz, born 1985 in Lima, currently in Buenos Aires.", True),

    # === True negatives: entities present but topically separated ====
    ("Alice Johnson published her paper. Boston is a vibrant city. "
      "The conference is in March 2024.", False),
    ("Bob Smith reviewed the work submitted. Berlin hosted the workshop. "
      "Records date back to 1972.", False),
    ("Carol Davis spoke at the panel. The discussion of San Francisco "
      "tech culture was unrelated. The year 1990 was mentioned.", False),
    ("In their 2020 paper, David Lee analyzed urbanization trends. "
      "Singapore was one example. The dataset extended back to 1985.", False),
    ("The keynote by Eve Martinez was insightful. Paris served as a case "
      "study. The historical analysis covered 1968 onwards.", False),
    ("Frank O'Brien presented findings. Dublin's economy is robust. "
      "1976 was a pivotal year for the industry.", False),
    ("Grace Kim's research is influential. Seoul plays a central role. "
      "Trends from 1985 are documented separately.", False),
    ("The paper by Henry Wilson addresses urban planning. Manchester is "
      "discussed in chapter 3. Brighton appears later. Data from 1955.", False),
    ("Iris Patel's analysis covers global trends. Mumbai is mentioned "
      "briefly. The date 1992 marks an inflection point.", False),
    ("James Brown's economic study is well-cited. Chicago school of "
      "thought is referenced. The data range starts in 1980.", False),
    ("Karen Lopez reviewed the literature. Madrid features in case study 4. "
      "Statistical context dates from 1979.", False),
    ("Leo Anderson edited the volume. Stockholm is chapter 7. The "
      "historical period covers 1965-2020.", False),
    ("Maria Rossi contributed Chapter 2. Rome is discussed independently. "
      "September 1988 marks a regulatory change.", False),
    ("Nadia Khan organized the panel. Lahore was the focus. Toronto "
      "perspective came in chapter 12. Surveys from 1991 forward.", False),
    ("Oliver Schmidt wrote the foreword. Munich and Frankfurt appear "
      "in different chapters. Data spans 1973 to today.", False),
    ("Pamela Adams reviewed the chapter. Sydney is the focus. May 1962 "
      "is referenced as a baseline year.", False),
    ("Quentin Mueller's contribution is in part II. Vienna and Zurich are "
      "compared in part III. The date 1987 is part of the timeline.", False),
    ("Rachel Green provided statistical analysis. Auckland is briefly "
      "noted. The figure for 1969 was the starting point.", False),
    ("Sam Thompson coordinated the project. Edinburgh hosted the event. "
      "April 1983 records were referenced.", False),
    ("In the paper authored by Tara Williams, Cardiff and London are "
      "compared as case studies. November 1975 is the earliest record.", False),
    ("The collaborative paper involved Uri Cohen as second author. "
      "Tel Aviv and several other cities were studied. 1981 is the "
      "starting date of the dataset.", False),
    ("Vera Petrov edited the volume. St. Petersburg is the subject of "
      "chapter 4. The database starts from 1986 records.", False),
    ("Walter Klein is acknowledged for advice. Hamburg and Frankfurt are "
      "compared separately. March 1968 marks the start of the period.", False),
    ("Ximena Lopez reviewed Chapter 5. Mexico City and Guadalajara are "
      "compared as test cases. The period studied is 1990 onwards.", False),
    ("Yusuf Ahmed contributed to the analysis. Cairo and Dubai are "
      "compared. The decade beginning 1977 is discussed in detail.", False),
    ("Zoe Bennett is acknowledged. Wellington appears once. February "
      "1984 is the dataset start.", False),

    # Long prose with isolated mentions (very loose; should not fire even
    # at large windows)
    ("This paper, authored by various contributors, addresses many "
      "topics. Among the authors is Alicia Reyes, who handled the "
      "statistical aspects. The paper covers Barcelona's economic "
      "development. We observed steady growth post-2015.", False),
    ("Brian Foster wrote section 1. The methodology section is detailed. "
      "Vancouver appears as one of the comparison cities. Demographic "
      "data covers individuals up to age 50 as of 2024.", False),
    ("The historical context is well-documented. Charles Hawking is "
      "thanked for a comment. The case study focuses on Cambridge. "
      "Data has been collected steadily since 1985.", False),
    ("Diana Pierce's contributions are in the appendix. Portland's "
      "policy framework is discussed in section 4. The year 1979 marks "
      "a significant shift in regulation.", False),
    ("Several authors contributed, including Edgar Wallace. The case "
      "studies in Manchester are detailed in Chapter 6. Demographic "
      "averages assume age 41 as the median.", False),
    ("This volume includes work by Fiona Stewart on methodology. "
      "Glasgow's industrial history is covered in Chapter 9. Records "
      "kept since 2010 inform the analysis.", False),
    ("In acknowledgments: Gerald Black for review, plus others. "
      "Liverpool and Bath are example cities. Records from 1958 and "
      "later are used.", False),
    ("Helena Voss is listed as a reviewer. Copenhagen is discussed in "
      "the European chapter. The cohort under study was born in 1992.", False),
    ("Ivan Petrov organized the workshop. Moscow served as the venue. "
      "Several countries' data from 1980 onward are analyzed.", False),
    ("Julia Schwartz reviewed the chapter. Brussels is one of three "
      "case-study cities. Demographic averages assume age 33 in 2024.", False),

    # Edge: only two of three entities, no full triple
    ("Alice Johnson and Bob Smith met in Boston.", False),  # no DATE
    ("The 1985 conference in Boston was historic.", False),  # no PERSON
    ("Alice Johnson was active in 1985.", False),  # no LOCATION
    ("She was born in 1985 and lives in Boston.", False),  # no PERSON name

    # Edge: triple in one sentence but in obvious non-PII context (history/news)
    ("Charles Dickens, born 1812, lived in London.", False),  # historical figure
    ("In 1969, Neil Armstrong set foot on the Moon.", False),  # public history
    ("Marie Curie was born in Warsaw in 1867.", False),  # historical
    ("Albert Einstein, born 1879, eventually lived in Princeton.", False),
    ("Mahatma Gandhi, born 1869 in Porbandar, led India to independence.", False),
]


# --- PERSON + ORGANIZATION (workplace identification) -------------------

PERSON_ORG = [
    # === True positives: identifies someone's workplace ==============
    ("Dr. Alice Johnson works at Massachusetts General Hospital.", True),
    ("Bob Smith is a senior engineer at Google.", True),
    ("Carol Davis joined Microsoft as a research scientist.", True),
    ("Prof. David Lee teaches at MIT.", True),
    ("Eve Martinez is the CFO at Tesla.", True),
    ("Frank O'Brien leads the team at OpenAI.", True),
    ("Grace Kim, a researcher at DeepMind, presented the work.", True),
    ("Henry Wilson is a partner at McKinsey.", True),
    ("Iris Patel directs the lab at Stanford.", True),
    ("James Brown is employed by IBM.", True),
    ("Karen Lopez serves as CTO of Anthropic.", True),
    ("Leo Anderson, formerly at Apple, joined Meta last year.", True),
    ("Maria Rossi is a tenured professor at Cambridge.", True),
    ("Nadia Khan works for Goldman Sachs.", True),
    ("Oliver Schmidt is a quantitative analyst at JP Morgan.", True),
    ("Pamela Adams holds a chair at Oxford.", True),
    ("Quentin Mueller is a VP at SpaceX.", True),
    ("Rachel Green has been with Pfizer for 10 years.", True),
    ("Sam Thompson recently joined Stripe as head of engineering.", True),
    ("Tara Williams works at the University of Edinburgh.", True),
    ("Uri Cohen is the founder of a fintech startup, FinTrust.", True),
    ("Vera Petrov is a consultant at Boston Consulting Group.", True),
    ("Walter Klein leads the AI division at Siemens.", True),
    ("Ximena Lopez is a senior researcher at Carnegie Mellon.", True),
    ("Yusuf Ahmed has worked at Reuters as a foreign correspondent.", True),
    ("Zoe Bennett is a partner at Latham & Watkins.", True),
    ("Alan Turing, while at Bletchley Park, broke the Enigma code.", True),  # named, but historical—context
    ("Beth Cooper is a software architect at Atlassian.", True),
    ("Carl Foster works at the World Health Organization.", True),
    ("Donna Hill recently took a position at Spotify.", True),
    ("Eric Vasquez is a managing director at Bain Capital.", True),
    ("Fred Owen is a research fellow at the National Institutes of Health.", True),
    ("Gina Bell has been a software engineer at Netflix since 2018.", True),
    ("Harry Singh is a portfolio manager at BlackRock.", True),
    ("Imogen Carter is the head of design at Airbnb.", True),
    ("Jasper Nguyen is a postdoctoral researcher at Caltech.", True),
    ("Kelly Burke is a vice president at Salesforce.", True),
    ("Liam Walsh works at the European Central Bank.", True),
    ("Mira Patel is a data scientist at Spotify.", True),
    ("Noah Beck is a senior trader at Citadel.", True),
    ("Olivia Reed is a research scientist at Mayo Clinic.", True),
    ("Patrick Doyle works as an in-house counsel at Apple.", True),
    ("Quinn Hayes is the head of product at Slack.", True),
    ("Renee Foster is a tenured faculty member at Yale.", True),
    ("Sebastian Vaughn works at the Federal Reserve.", True),
    ("Tom Reagan is a senior scientist at AstraZeneca.", True),
    ("Una Marshall has worked at the BBC for 15 years.", True),
    ("Victor Lin is the principal architect at Cloudflare.", True),
    ("Wendy Park works at the World Bank.", True),
    ("Xander Wright is a product manager at Adobe.", True),
    ("Yvonne Tate joined Twitter in 2019 as VP of engineering.", True),

    # === True negatives: PERSON and ORG mentioned but unrelated =====
    ("Alice Johnson published a paper. The Google scholar citation count "
      "is 200.", False),
    ("Bob Smith reviewed the work. Microsoft Research was thanked in the "
      "acknowledgments.", False),
    ("Carol Davis presented her ideas. Apple's app store was used as one "
      "of many examples.", False),
    ("Prof. David Lee's keynote covered many topics. MIT Press published "
      "the proceedings.", False),
    ("Eve Martinez authored chapter 3. Tesla was discussed as an industry "
      "case study elsewhere in the book.", False),
    ("Frank O'Brien gave the closing remarks. OpenAI's GPT-4 was "
      "referenced in the technical appendix.", False),
    ("Grace Kim moderated the panel. DeepMind's AlphaFold appeared in the "
      "discussion of computational biology.", False),
    ("Henry Wilson chaired the session. McKinsey's reports are widely "
      "cited in business strategy literature.", False),
    ("Iris Patel introduced the speakers. Stanford University as an "
      "institution has a long history.", False),
    ("James Brown organized the conference. IBM's cognitive computing "
      "platform was demoed by another team.", False),
    ("In the chapter by Karen Lopez, Anthropic's safety research is "
      "discussed alongside several other companies' work.", False),
    ("Leo Anderson contributed methodology. Apple and Meta are compared "
      "as competitors in chapter 8 by other authors.", False),
    ("Maria Rossi reviewed the volume. Cambridge University Press is "
      "the publisher.", False),
    ("Nadia Khan's literature review is comprehensive. Goldman Sachs's "
      "annual reports are cited as evidence in section 4.", False),
    ("Oliver Schmidt's mathematical framework is detailed in chapter 2. "
      "JP Morgan's trading desk operations are discussed separately.", False),
    ("Pamela Adams's chapter is on theory. Oxford University and Cambridge "
      "are mentioned as historical institutions.", False),
    ("Quentin Mueller's economic analysis is in part 1. SpaceX's launch "
      "manifest informs part 4.", False),
    ("Rachel Green's discussion is methodological. Pfizer's vaccine "
      "development is one of many case studies.", False),
    ("Sam Thompson is acknowledged for technical review. Stripe's API "
      "design is briefly referenced as an example.", False),
    ("Tara Williams's analysis sets the stage. The University of "
      "Edinburgh's library is acknowledged for archival access.", False),
    ("Uri Cohen wrote the historical chapter. FinTrust is one of "
      "several startups profiled in chapter 5.", False),
    ("Vera Petrov reviewed the document. Boston Consulting Group's "
      "frameworks are referenced in the strategy section.", False),
    ("Walter Klein moderated the discussion. Siemens's energy division "
      "is discussed in a separate case study.", False),
    ("Ximena Lopez wrote the methodology chapter. Carnegie Mellon's CS "
      "department is mentioned among many institutions.", False),
    ("Yusuf Ahmed prepared the bibliography. Reuters is cited as a news "
      "source for several events.", False),
    ("Zoe Bennett reviewed the legal section. Latham & Watkins is one of "
      "many firms discussed in the appendix.", False),

    # Mentions of organizations where person isn't an employee
    ("Alice Johnson criticized Google's recent privacy changes.", False),
    ("Bob Smith filed a complaint against Microsoft.", False),
    ("Carol Davis sued Apple over a patent dispute.", False),
    ("David Lee's research uses datasets from MIT.", False),
    ("Eve Martinez's car was a Tesla.", False),
    ("Frank O'Brien used OpenAI's API for his project.", False),
    ("Grace Kim cited a paper from DeepMind.", False),
    ("Henry Wilson commented on McKinsey's report.", False),
    ("Iris Patel disagrees with Stanford's policy.", False),
    ("James Brown bought stock in IBM.", False),
    ("Anthropic's policy on Karen Lopez's complaint was unclear.", False),

    # Edge: PERSON without ORG, ORG without PERSON
    ("Alice Johnson is a researcher.", False),
    ("Google released a new product yesterday.", False),
    ("Microsoft's revenue grew this quarter.", False),

    # Historical / public-figure / news context (PERSON+ORG present but
    # not actionable PII concern in the GDPR sense)
    ("Tim Cook leads Apple as CEO.", False),  # public information
    ("Sam Altman runs OpenAI.", False),  # public
    ("Elon Musk founded SpaceX.", False),  # public
]


# --- PERSON + AGE / DATE_TIME-as-age (age-tied identification) ----------

PERSON_AGE = [
    # === True positives: PERSON adjacent to age/birth-year ==========
    ("Alice Johnson, 42, said she was retiring soon.", True),
    ("Bob Smith (39) attended the workshop.", True),
    ("Carol Davis, age 51, was the keynote speaker.", True),
    ("David Lee, 28, just got promoted.", True),
    ("Eve Martinez, born in 1982, has three children.", True),
    ("Frank O'Brien, born 1975, said he had been working there for 20 years.", True),
    ("Grace Kim, 33, is the youngest member of the board.", True),
    ("Henry Wilson (62) recently retired.", True),
    ("Iris Patel, age 47, leads the team.", True),
    ("James Brown, 55 years old, has decades of experience.", True),
    ("Karen Lopez, b. 1990, is the new hire.", True),
    ("Leo Anderson, 71, has been writing memoirs.", True),
    ("Maria Rossi, age 38, is the team captain.", True),
    ("Nadia Khan, 26, just won the prize.", True),
    ("Oliver Schmidt (43) is the technical lead.", True),
    ("Pamela Adams, born 1962, took early retirement.", True),
    ("Quentin Mueller, age 34, joined the firm last week.", True),
    ("Rachel Green, 45, was elected chair.", True),
    ("Sam Thompson, born December 1983, leads the team.", True),
    ("Tara Williams, 49, is the new president.", True),
    ("Uri Cohen (41) opened the conference.", True),
    ("Vera Petrov, born 1987, is the youngest partner.", True),
    ("Walter Klein, age 56, moderated the panel.", True),
    ("Ximena Lopez, 30, has been promoted again.", True),
    ("Yusuf Ahmed, 47, is the foreign correspondent.", True),
    ("Zoe Bennett, b. February 1985, is the lead author.", True),
    ("Alicia Reyes, age 35, gave the keynote.", True),
    ("Brian Foster (50) is on the leadership team.", True),
    ("Charles Hawking, 67, is professor emeritus.", True),
    ("Diana Pierce, born 1979, joined Apple recently.", True),
    ("Edgar Wallace, 41, is the project manager.", True),
    ("Fiona Stewart, age 40, contributed the data section.", True),
    ("Gerald Black (64) is the senior partner.", True),
    ("Helena Voss, 33, was the first to volunteer.", True),
    ("Ivan Petrov, born 1980, is the chief architect.", True),
    ("Julia Schwartz, age 33, made the announcement.", True),
    ("Kevin Murphy, 53, was thanked publicly.", True),
    ("Linda Carter (35) is the new MD.", True),
    ("Michael Stone, age 58, is taking sabbatical.", True),
    ("Natalie Wong, born 1988, was honored.", True),
    ("Omar Hassan, 38, has joined the steering committee.", True),
    ("Patricia Bell, age 50, is one of the longest-serving employees.", True),
    ("Quinn O'Hara, 31, was promoted last month.", True),
    ("Ruth Allen (44) heads the new initiative.", True),
    ("Steven Chang, 52, is the senior engineer.", True),
    ("Tina Roberts, age 45, is the chief medical officer.", True),
    ("Ulrich Bach, born 1969, is the firm's senior counsel.", True),
    ("Victoria Hayes, 34, is the head of marketing.", True),
    ("William Dunn, age 60, is retiring soon.", True),
    ("Xavier Cruz, 39, was the first hire in the new office.", True),
    ("Yara Khalil, born 1991, is on the editorial board.", True),

    # === True negatives: PERSON without age proximity ===============
    ("Alice Johnson, an experienced engineer, joined the project. "
      "Workforce statistics show the median age is 42.", False),
    ("Bob Smith presented his findings. The dataset includes 39 records "
      "from various sources.", False),
    ("Carol Davis spoke at the gala. The event raised $51 million.", False),
    ("David Lee published a paper. The journal is on its 28th volume.", False),
    ("Eve Martinez served on the panel. The historical analysis covers "
      "1982 to today.", False),
    ("Frank O'Brien moderated the discussion. The 1975 oil shock was "
      "one of many examples discussed.", False),
    ("Grace Kim won the award. The 33rd annual ceremony was held last "
      "month.", False),
    ("Henry Wilson contributed to the volume. The historical scope "
      "covers the years 1962 to 2020.", False),
    ("Iris Patel discussed leadership. The team has 47 members.", False),
    ("James Brown wrote the foreword. The book has 55 chapters.", False),
    ("Karen Lopez reviewed the manuscript. The dataset begins in 1990.", False),
    ("Leo Anderson edited the volume. The book has 71 contributors.", False),
    ("Maria Rossi gave the closing remarks. The conference attracted "
      "38 sponsors.", False),
    ("Nadia Khan organized the panel. The agenda had 26 items.", False),
    ("Oliver Schmidt presented at the symposium. There were 43 attendees "
      "total.", False),
    ("Pamela Adams's keynote was well-received. The historical focus on "
      "1962 was noted by several reviewers.", False),
    ("Quentin Mueller's introduction set the tone. The 34th international "
      "meeting concluded successfully.", False),
    ("Rachel Green provided context. The committee has 45 active members.", False),
    ("Sam Thompson coordinated logistics. The dataset starts December "
      "1983.", False),
    ("Tara Williams's analysis was rigorous. The journal has 49 published "
      "issues this year.", False),

    # Edge: clear separation of PERSON and number-that-could-be-age
    ("Alice Johnson is a researcher. There are 42 papers in the corpus.", False),
    ("Bob Smith published widely. He authored 39 articles last decade.", False),
    ("Carol Davis chaired the meeting. The 51st annual gathering was a "
      "success.", False),
    ("David Lee was elected. He won by a margin of 28 votes.", False),

    # Public-figure age (not actionable PII) - low concern
    ("Joe Biden, 81, gave a speech.", False),
    ("Donald Trump, 78, addressed the rally.", False),
    ("Elizabeth II, age 96 at the time, opened parliament.", False),
]


# --- PERSON + MEDICAL_LICENSE (professional identification) -------------
#
# Note: MEDICAL_LICENSE is a Presidio entity covering medical license
# numbers. The combined-inference signal is "named medical professional
# with license number" — strong identification of a specific licensed
# practitioner. Most samples are synthetic since real license numbers
# are PII.

PERSON_MEDICAL = [
    # === True positives: PERSON + license number ===================
    ("Dr. Alice Johnson, license A-12345, signed the prescription.", True),
    ("Bob Smith, MD, NPI 1234567890, is the attending physician.", True),
    ("Dr. Carol Davis (license CA-98765) approved the treatment.", True),
    ("David Lee, RN, license RN-2024-0001, completed the rounds.", True),
    ("Dr. Eve Martinez, NPI 9876543210, is the consulting specialist.", True),
    ("The note was signed by Frank O'Brien, MD, license MD-456789.", True),
    ("Grace Kim, license # NY-12345-PA, performed the procedure.", True),
    ("Dr. Henry Wilson, DEA: BH1234567, prescribed the medication.", True),
    ("Iris Patel, MD (license IL-887766), saw the patient.", True),
    ("Attending: Dr. James Brown, NPI 5432167890.", True),

    # === True negatives: license number without nearby PERSON ======
    ("The medical license requirements changed last year. "
      "Specifications include a registration number like MD-456789 "
      "as the standard format. Alice Johnson reviewed the policy.", False),
    ("State licensing for nurses requires a number such as RN-2024-0001. "
      "Bob Smith's analysis of policy was informative.", False),
    ("NPI numbers are 10 digits like 1234567890 by federal standard. "
      "Carol Davis chaired the working group.", False),
    ("DEA license format BH1234567 follows a specific schema. "
      "David Lee co-authored the documentation.", False),
    ("The license format A-12345 is used in some states. "
      "Eve Martinez wrote the comparison report.", False),

    # Edge cases
    ("Dr. Alice Johnson is the attending physician.", False),  # no license
    ("License MD-456789 is expired.", False),  # no person
    ("The NPI 1234567890 belongs to a clinic.", False),  # no specific person
]


# --- expose all corpora --------------------------------------------------

CORPORA = {
    "PERSON_LOCATION_DATE": PERSON_LOCATION_DATE,
    "PERSON_ORG": PERSON_ORG,
    "PERSON_AGE": PERSON_AGE,
    "PERSON_MEDICAL": PERSON_MEDICAL,
}


def stats() -> dict:
    """Return per-corpus counts for sanity checking."""
    return {
        name: {
            "total": len(samples),
            "positive": sum(1 for _, label in samples if label),
            "negative": sum(1 for _, label in samples if not label),
        }
        for name, samples in CORPORA.items()
    }


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))
