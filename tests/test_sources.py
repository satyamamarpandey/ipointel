from app.services.sec import parse_price_range,parse_atom
from app.services.nse import normalize,archive_links

def test_sec_price_range():
    assert parse_price_range('The initial public offering price is expected to be between $14.00 and $16.00 per share.')==(14.0,16.0)

def test_nse_normalize():
    x=normalize({'companyName':'ABC Ltd','symbol':'ABC','priceBandMin':'95','priceBandMax':'100','noOfSharesOffered':'1000000','noOfSharesBid':'5000000','marketLot':'150'},'Open')
    assert x['company']=='ABC Ltd' and x['total_sub']==5 and x['price_high']==100 and x['lot_size']==150

def test_archive_links():
    html='<a href="https://nsearchives.nseindia.com/a.xlsx">Primary Market Monthly Report - July 2026 (.xlsx)</a>'
    assert archive_links(html)[0][1].endswith('a.xlsx')

def test_parse_date_handles_sec_master_index_format():
    from app.services.market import parse_date
    d = parse_date('20260819')
    assert d is not None and d.year == 2026 and d.month == 8 and d.day == 19
    assert parse_date('2026-08-19').day == 19
    assert parse_date('') is None

def test_latest_fact_reads_dei_shares_outstanding():
    from app.services.sec import latest_fact
    facts={"facts":{"dei":{"EntityCommonStockSharesOutstanding":{"units":{"shares":[
        {"val":50_000_000,"filed":"2026-01-01","end":"2025-12-31"},
        {"val":52_500_000,"filed":"2026-03-01","end":"2026-02-28"},
    ]}}}}}
    shares_m=latest_fact(facts,["EntityCommonStockSharesOutstanding"],taxonomies=("dei",))
    assert round(shares_m,2)==52.5

def test_sec_master_and_priced_filter():
    from app.services.sec import parse_master_index, parse_priced_ipo
    text='CIK|Company Name|Form Type|Date Filed|Filename\n123|Example Inc|424B4|2026-08-20|edgar/data/123/a.txt\n'
    rows=parse_master_index(text);assert rows[0]['cik']=='123' and rows[0]['filing_url'].startswith('https://www.sec.gov/Archives/')
    assert parse_priced_ipo('This prospectus describes our initial public offering. The initial public offering price is $18.00 per share. Trading on Nasdaq under the symbol EXMP.')['final_price']==18.0
    assert parse_priced_ipo('This is a secondary offering by existing shareholders only.') is None


# ---------- EDGAR title parsing: the form's own hyphen is not the separator ----------

def _atom(*titles):
    entries = "".join(
        f'<entry><title>{t}</title><updated>2026-09-01T00:00:00-04:00</updated>'
        f'<link href="https://www.sec.gov/x"/></entry>' for t in titles)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


def test_form_number_is_not_glued_onto_the_company_name():
    """"S-1 - ACME, INC. (...)" must yield "ACME, INC.", not "1 - ACME, INC.".
    The old `^[^-]+-` stopped at the hyphen inside the form name itself."""
    rows = parse_atom(_atom('S-1 - ADAPTIN BIO, INC. (0001938571) (Filer)'), 'S-1')
    assert rows[0]['company'] == 'ADAPTIN BIO, INC.'
    assert rows[0]['cik'] == '1938571'


def test_two_digit_form_suffix_is_not_glued_on_either():
    rows = parse_atom(_atom('S-11 - Graf Industrial Corp. II (0002113088) (Filer)'), 'S-11')
    assert rows[0]['company'] == 'Graf Industrial Corp. II'


def test_amendment_forms_parse_to_the_bare_company_name():
    """EDGAR's getcurrent feed matches on form prefix, so a type=S-1 query
    also returns S-1/A entries - those produced "1/A - ACME"."""
    rows = parse_atom(_atom('S-1/A - Youmi Inc. (0001960864) (Filer)'), 'S-1')
    assert rows[0]['company'] == 'Youmi Inc.'
    rows = parse_atom(_atom('F-1/A - RZ Wellness Ltd (0002051391) (Filer)'), 'F-1')
    assert rows[0]['company'] == 'RZ Wellness Ltd'


def test_company_names_containing_hyphens_survive_intact():
    rows = parse_atom(_atom('S-1 - 1-800-FLOWERS.COM, INC. (0001084869) (Filer)'), 'S-1')
    assert rows[0]['company'] == '1-800-FLOWERS.COM, INC.'


# ---------- stored-name cleanup ----------

def test_clean_company_name_strips_the_stored_form_artifact():
    from app.services.pipeline import clean_company_name
    assert clean_company_name('1 - Reliance Global Group, Inc.', 'United States') == 'Reliance Global Group, Inc.'
    assert clean_company_name('11 - Wheeler Real Estate Investment Trust, Inc.', 'United States') == 'Wheeler Real Estate Investment Trust, Inc.'
    assert clean_company_name('1/A - Youmi Inc.', 'United States') == 'Youmi Inc.'


def test_clean_company_name_never_damages_a_real_numeric_name():
    """The required spaces around the hyphen are what separate the artifact
    from a genuine name - these must come through untouched."""
    from app.services.pipeline import clean_company_name
    for name in ('1-800-FLOWERS.COM, INC.', '3i Infotech Limited', '5paisa Capital Limited',
                 '360 ONE WAM LIMITED', '23andMe Holding Co.'):
        assert clean_company_name(name, 'United States') == name


def test_clean_company_name_normalises_scraped_whitespace():
    from app.services.pipeline import clean_company_name
    assert clean_company_name('\tGlobe International Carriers Limited', 'India') == 'Globe International Carriers Limited'
    assert clean_company_name('\nValiant Laboratories Limited', 'India') == 'Valiant Laboratories Limited'
    assert clean_company_name(' Nazara Technologies Ltd\t', 'India') == 'Nazara Technologies Ltd'
    assert clean_company_name('Bannari  Amman   Spinning Mills', 'India') == 'Bannari Amman Spinning Mills'


def test_indian_names_are_not_subject_to_the_sec_artifact_rule():
    """An Indian issuer named like the artifact must keep its name - the rule
    is SEC-specific because that is the only source that produces it."""
    from app.services.pipeline import clean_company_name
    assert clean_company_name('1 - Some India Ltd', 'India') == '1 - Some India Ltd'
