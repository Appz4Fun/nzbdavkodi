import pytest
from ptt.transformers import convert_months

def test_convert_months_basic():
    """Test basic conversion of long month names to short ones."""
    assert convert_months("20 Janu 2020") == "20 Jan 2020"
    assert convert_months("15 Febr 2021") == "15 Feb 2021"

def test_convert_months_case_insensitive():
    """Test that the conversion is case-insensitive."""
    assert convert_months("20 jAnU 2020") == "20 Jan 2020"
    assert convert_months("15 fEbR 2021") == "15 Feb 2021"

def test_convert_months_all_months():
    """Test that all month prefixes in the mapping are correctly converted."""
    months = [
        ("Janu", "Jan"), ("Febr", "Feb"), ("Marc", "Mar"),
        ("Apri", "Apr"), ("May", "May"), ("June", "Jun"),
        ("July", "Jul"), ("Augu", "Aug"), ("Sept", "Sep"),
        ("Octo", "Oct"), ("Nove", "Nov"), ("Dece", "Dec")
    ]
    for long_month, short_month in months:
        assert convert_months(f"01 {long_month} 2020") == f"01 {short_month} 2020"

def test_convert_months_no_match():
    """Test scenarios where no conversion should occur."""
    assert convert_months("01 January 2020") == "01 January 2020"
    assert convert_months("01 Jan 2020") == "01 Jan 2020"
    assert convert_months("random string") == "random string"
    assert convert_months("") == ""
