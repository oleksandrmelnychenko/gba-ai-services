#!/usr/bin/env python3
"""Generate/check exact native report SQL fragments. No database access or hand-maintained proof.

Release gate: --check --server-repo <reviewed gba-server checkout>. A canonical source
change requires explicit regeneration plus NativeSalesNet live parity validation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import textwrap

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app/data/canonical_sql"
REPOSITORIES = Path("src/Global.Business.Assistant.Operations.Infrastructure.Persistence/Report/Repositories")
SOURCES = ("SalesReportProjectionRepository.cs", "NativeSalesNetReportProjectionRepository.cs",
           "NativeReturnCurrencyEvidenceSql.cs", "NativeReportDecimalSql.cs")


def raw_constant(source, name):
    match = re.search(r'\b' + name + r'\s*=\s*"""\n(.*?)\n\s*""";', source, re.S)
    if not match:
        raise ValueError(f"Canonical raw constant not found: {name}")
    return textwrap.dedent(match.group(1))


def concatenated(source, start, end):
    fragment = source.split(start, 1)[1].split(end, 1)[0]
    fragment = re.sub(r'//[^\n]*', '', fragment)
    literals = re.findall(r'\$?"((?:[^"\\]|\\.)*)"', fragment)
    if not literals:
        raise ValueError("Canonical concatenated SQL changed shape")
    return ''.join(json.loads('"' + value + '"') for value in literals)


def generate(server_repo):
    sources = {name: (server_repo / REPOSITORIES / name).read_text() for name in SOURCES}
    gross = sources[SOURCES[0]]
    net = sources[SOURCES[1]]
    currency = sources[SOURCES[2]]
    product = concatenated(sources[SOURCES[3]], 'Product(string price, string quantity) =>', ';')
    gross_from = concatenated(gross, 'string factFrom =', ';')
    gross_where = concatenated(gross, 'string factWhere =', '+ filters;')
    return_from = raw_constant(net, 'ReturnFromSql')
    # Cost/VAT-only joins are not part of the gross-with-VAT monetary fact contract.
    # Retain the canonical active item and unique confirmed source-sale binding verbatim.
    return_money_from = return_from.split('LEFT JOIN returnCosts rc ', 1)[0].rstrip()
    currency_join = next(line for line in return_from.splitlines() if 'LEFT JOIN returnCurrencyConfirmed currency ' in line)
    if 'LEFT JOIN returnCurrencyConfirmed currency ' not in return_money_from:
        return_money_from += '\n' + currency_join
    value = re.search(r'SUM\((CASE WHEN sr\.SourceDocumentID IS NULL OR currency\.SaleReturnID IS NOT NULL.*?END)\) AS ValueWithVat', net, re.S)
    if not value:
        raise ValueError('Canonical return amount expression changed shape')
    unresolved = re.search(r'SUM\(CAST\((CASE WHEN sr\.SourceDocumentID IS NOT NULL AND currency\.SaleReturnID IS NULL.*?END) AS bigint\)\) AS ValueUnresolvedLines', net, re.S)
    if not unresolved:
        raise ValueError('Canonical unresolved currency expression changed shape')
    artifacts = {
        'sales_from.sql': gross_from,
        'sales_where.sql': gross_where,
        'decimal_product.sql': product,
        'return_from.sql': return_money_from,
        'return_value.sql': textwrap.dedent(value.group(1)),
        'return_unresolved.sql': textwrap.dedent(unresolved.group(1)),
        'return_currency.sql': ', returnCurrencyDocuments AS (\n' + raw_constant(currency, 'DocumentsQuery') + '\n)'
                               + raw_constant(currency, 'DocumentCtes'),
    }
    artifacts = {name: value.rstrip() + '\n' for name, value in artifacts.items()}
    manifest = {
        'contract': 'native-confirmed-gross-and-active-returns-with-vat-v1',
        'source_files': {str(REPOSITORIES / name): hashlib.sha256(value.encode()).hexdigest() for name, value in sources.items()},
        'artifacts': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in artifacts.items()},
        'notes': ['SQL fragments are extracted from native report source, with no currency-proof simplification.',
                  'Runtime only replaces named date bindings; gross and return dates use separate Kyiv UTC boundaries.',
                  'Return cost/VAT joins omitted because only gross EUR and returned quantity are requested.',
                  'Regeneration requires unit tests and live parity against NativeSalesNet on identical scopes.'],
    }
    artifacts['manifest.json'] = json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'
    return artifacts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server-repo', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    artifacts = generate(args.server_repo)
    if args.check:
        mismatches = [name for name, content in artifacts.items()
                      if not (OUTPUT / name).is_file() or (OUTPUT / name).read_text() != content]
        if mismatches:
            raise SystemExit('Canonical source/artifact drift: ' + ', '.join(mismatches))
        print('Canonical sales SQL artifacts and source hashes match.')
    else:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts.items():
            (OUTPUT / name).write_text(content)
        print(f'Generated {len(artifacts) - 1} canonical SQL fragments and SHA256 manifest.')


if __name__ == '__main__':
    main()
