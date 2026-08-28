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

def test_sec_master_and_priced_filter():
    from app.services.sec import parse_master_index, parse_priced_ipo
    text='CIK|Company Name|Form Type|Date Filed|Filename\n123|Example Inc|424B4|2026-08-20|edgar/data/123/a.txt\n'
    rows=parse_master_index(text);assert rows[0]['cik']=='123' and rows[0]['filing_url'].startswith('https://www.sec.gov/Archives/')
    assert parse_priced_ipo('This prospectus describes our initial public offering. The initial public offering price is $18.00 per share. Trading on Nasdaq under the symbol EXMP.')['final_price']==18.0
    assert parse_priced_ipo('This is a secondary offering by existing shareholders only.') is None
