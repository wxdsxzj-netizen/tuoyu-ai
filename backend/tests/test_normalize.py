from app.data.normalize import normalize_company_name, normalize_country, normalize_domain, normalize_email


def test_company_name():
    assert normalize_company_name("ACME Trading Co., Ltd.") == "acme trading"
    assert normalize_company_name("Müller Intl GmbH") == "mueller international"
    assert normalize_company_name("Foo S.p.A.") == "foo"


def test_domain():
    assert normalize_domain("https://WWW.Example.com/about?x=1") == "example.com"
    assert normalize_domain("example.de") == "example.de"
    assert normalize_domain("john@sub.example.com") == "sub.example.com"
    assert normalize_domain("not a domain") is None


def test_email():
    assert normalize_email("J.Doe+tag@GMAIL.com") == "jdoe@gmail.com"
    assert normalize_email("A.B@corp.com") == "a.b@corp.com"
    assert normalize_email("bad@") is None


def test_country():
    assert normalize_country("de") == "DE"
    assert normalize_country("Germany") == "DE"
    assert normalize_country("德国") == "DE"
    assert normalize_country("USA") == "US"
    assert normalize_country("DEU") == "DE"
    assert normalize_country("Atlantis") is None
