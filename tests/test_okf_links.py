from swarph_cli.commands.okf_links import parse_okf_links


def test_grammar_table():
    assert parse_okf_links("[[a]]") == ["a"]
    assert parse_okf_links("[[a|Alias A]]") == ["a"]               # alias dropped
    assert parse_okf_links("[[a#Heading]]") == ["a"]               # heading dropped
    assert parse_okf_links("![[embed]]") == ["embed"]             # transclusion is an edge
    assert parse_okf_links("see [txt](notes/b.md)") == ["notes/b.md"]  # md link
    # combined, order-preserving dedupe, markdown non-.md links ignored
    assert parse_okf_links("[[a]] x [[a|z]] y [[c#h]] z [q](http://x)") == ["a", "c"]


def test_empty_and_none():
    assert parse_okf_links("") == []
    assert parse_okf_links(None) == []
