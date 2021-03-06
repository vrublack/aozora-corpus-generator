import sys
import re
import unicodedata
import jaconv
import csv

from collections import (
    OrderedDict,
    defaultdict,
)
from typing import (
    Dict,
    List,
    Tuple,
)

from zipfile import ZipFile
from os.path import join, splitext
from io import TextIOWrapper
import html5_parser as html
from natto import MeCab

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)


def make_jis_unicode_map(file_path: str) -> Dict[str, str]:
    '''
    Generates a translation dictionary between the men-ku-ten
    (i.e. '1-1-24') type of representation of characters in the JIS X
    0213 standard and Unicode. This format is used to represent most
    of the so-called 'gaiji' within Aozora Bunko, which refer to
    characters on the 3rd and 4th planes of the JIS X 0213
    standard. Note that this does not cover all 'gaiji' use, which
    includes references to Unicode itself or to a decription of the
    character as combination of two or more other chracters.
    Reference: http://www.aozora.gr.jp/annotation/external_character.html
    '''
    d = {}
    hex_to_code = dict(zip([format(i, 'X') for i in range(33, 33+95)],
                           ['{0:0>2}'.format(i) for i in range(1, 95)]))

    with open(file_path) as f:
        for line in f:
            if line[0] == '#':
                continue

            jis_field, unicode_field = line.split('\t')[0:2]

            jis_standard, jis_code = jis_field.split('-')
            if jis_standard == '3':
                men = 1
            elif jis_standard == '4':
                men = 2

            ku = hex_to_code[jis_code[0:2]]
            ten = hex_to_code[jis_code[2:4]]

            unicode_point = unicode_field.replace('U+', '')
            if unicode_point == '':  # No mapping exists.
                continue
            elif len(unicode_point) > 6:  # 2 characters
                first, second = unicode_point.split('+')
                unicode_char = chr(int(first, 16)) + chr(int(second, 16))
            else:
                unicode_char = chr(int(unicode_point, 16))

            jis_string = '{}-{}-{}'.format(men, ku, ten)

            d[jis_string] = unicode_char
    return d
# TODO ／″＼ and Unidic


def make_ndc_map():
    with open('ndc-3digits.tsv') as f:
        d = {}
        for line in f:
            code, label = line.rstrip('\n').split('\t')
            d[code] = label
        return d


def normalize_japanese_text(s):
    '''
    Normalizes to NFKC Unicode norm and converts all half-width
    alphanumerics and symbols to full-width. The latter step is needed
    for correct use with the UniDic dictionary used with MeCab.
    '''
    return jaconv.h2z(unicodedata.normalize('NFKC', s),
                      kana=False,
                      digit=True,
                      ascii=True).replace('.', '．').replace(',', '，')


def split_sentence_ja(s):
    '''
    Splits Japanese sentence on common sentence delimiters. Some care
    is taken to prevent false positives, but more sophisticated
    (non-regex) logic would be required to implement a more robust
    solution.
    '''
    delimiters = r'[!\?。．！？…]'
    closing_quotations = r'[\)）」』】］〕〉》\]]'
    return [s.strip() for s in re.sub(r'({}+)(?!{})'.format(delimiters, closing_quotations),
                                      r'\1\n',
                                      s).splitlines()
            if s.strip()]


KATAKANA = set(list('ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソ'
                    'ゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペ'
                    'ホボポマミムメモャヤュユョヨラリルレロワヲンーヮヰヱヵヶヴ'))

HIRAGANA = set(list('ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすず'
                    'せぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴ'
                    'ふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろわ'
                    'をんーゎゐゑゕゖゔ'))

KANJI_RX = re.compile(r'[\u4e00-\u9fff]')

def code_frequencies(text):
    cmap = {
        'katakana': 0,
        'hiragana': 0,
        'kanji': 0,
        'other': 0,
    }

    unigram_types = defaultdict(int)

    for c in text:
        unigram_types[c] += 1
        if c in KATAKANA:
            cmap['katakana'] += 1
        elif c in HIRAGANA:
            cmap['hiragana'] += 1
        elif KANJI_RX.match(c):
            cmap['kanji'] += 1
        else:
            cmap['other'] += 1

    return cmap, unigram_types


def is_katakana_sentence(text, tokens):
    '''
    Identify if sentence is made up of katakana (ie. no hiragana)
    characters. In such a case, it should be converted to hiragana,
    processed, and then the original orth should be substituted back
    in.

    Note: This identification is an imperfect heuristic and should
    be replaced.
    '''

    cmap, unigram_types = code_frequencies(text)

    bigram_types = defaultdict(int)
    for bigram in map(lambda a, b: '{}{}'.format(a, b), text, text[1:]):
        bigram_types[bigram] += 1

    katakana_ratio = cmap['katakana'] / sum(cmap.values())

    if cmap['hiragana'] == 0 and katakana_ratio > 0.5:

        oov_count = sum(1 for token in tokens if token['oov'])
        proper_noun_chars = sum(len(token['orth']) for token in tokens
                                if token['pos2'] == '固有名詞')

        if oov_count == 0 and proper_noun_chars / len(text) > 0.3:
            return False

        if oov_count / len(tokens) > 0.2 or \
           len(text) < 8 or \
           (len(text) < 10 and re.search(r'ッ?.?[？！]」$', text[-3:])) or \
           (len(text) < 100 and len(unigram_types) / len(text) < 0.5) or \
           max(bigram_types.values()) / len(text) > 0.5 or \
           len(re.findall(r'(.)\1+', text)) / len(text) > 0.1 or \
           len(re.findall(r'(..)ッ?\1', text)) / len(text) > 0.1:
            return False
        return True
    else:
        return False


def sentence_to_tokens(sentence, is_katakana=False):
    '''
    Parses one sentence into tokens using MeCab. Assumes UniDic CWJ
    2.2.0 version of the dictionary is set as default. If is_katakana
    is set, then will convert hiragana to katakana before passing the
    string to MeCab and then finally reverting the change to the
    surface form.
    '''
    unidic_features = [
        'pos1', 'pos2', 'pos3', 'pos4', 'cType', 'cForm', 'lForm', 'lemma',
        'orth', 'pron', 'orthBase', 'pronBase', 'goshu', 'iType', 'iForm',
        'fType', 'fForm', 'iConType', 'fConType', 'type', 'kana', 'kanaBase',
        'form', 'formBase', 'aType', 'aConType', 'aModType', 'lid', 'lemma_id'
    ]
    tokens = []

    if is_katakana:
        sentence = jaconv.kata2hira(sentence)

    with MeCab() as mecab:
        for node in mecab.parse(sentence, as_nodes=True):
            if not node.is_eos():
                token = dict(zip(unidic_features, node.feature.split(',')))

                token['pos'] = token['pos1']
                if token['pos2'] != '*':
                    token['pos'] += '-' + token['pos2']
                if token['pos3'] != '*':
                    token['pos'] += '-' + token['pos3']
                if token['pos4'] != '*':
                    token['pos'] += '-' + token['pos4']

                if len(token) == 7:  # OOV
                    if is_katakana:
                        token['orth'] = jaconv.hira2kata(node.surface)
                    else:
                        token['orth'] = node.surface
                    token['orthBase'] = node.surface
                    token['lemma'] = node.surface
                    token['oov'] = True
                    tokens.append(token)
                else:
                    if is_katakana:
                        token['orth'] = jaconv.hira2kata(token['orth'])
                    token['oov'] = False
                    tokens.append(token)
    return tokens


SPEECH_RX = re.compile(r'「(.+)」')


def text_to_tokens(text, speech_mode='yes'):
    '''
    Returns a sequence of sentences, each comprising a sequence of
    tokens. Must be subsumed into non-lazy collection.  Will re-parse
    the sentence if it detects it as written in kanji-katakana-majiri
    form. speech_mode controls the presence or absence of direct
    speech.
    '''
    for sentence in split_sentence_ja(text):
        if speech_mode != 'yes':
            if SPEECH_RX.search(sentence):
                s = sentence
                if speech_mode == 'speech':
                    s = ''
                for maybe_speech in SPEECH_RX.findall(sentence):
                    cmap, _ = code_frequencies(maybe_speech)
                    if cmap['kanji'] == sum(cmap.values()):
                        # False positive. (crude detection heuristic)
                        # Ideally test for minimal sentence...
                        continue
                    # TODO: output as tagged data
                    if speech_mode == 'no':
                        s = s.replace('「{}」'.format(maybe_speech), '')
                    elif speech_mode == 'narrative':
                        s = ''
                        break
                    elif speech_mode == 'speech':
                        s += '「{}」'.format(maybe_speech)
                sentence = s
            elif speech_mode == 'speech':
                continue

        tokens = sentence_to_tokens(sentence)
        if len(tokens) == 0:
            continue

        is_katakana = is_katakana_sentence(sentence, tokens)
        if is_katakana:
            tokens = sentence_to_tokens(sentence, is_katakana)

        yield tokens


PUNC_RX = re.compile(r'^((補助)?記号|空白)$')
NUMBER_RX = re.compile(r'^[\d０-９一-九]+$')


def wakati(text, no_punc=True, speech_mode='yes'):
    '''
    Returns a sequence of sentences comprised of whitespace separated tokens.
    '''
    for sentence in text_to_tokens(text, speech_mode):
        if no_punc:
            yield [token['orth'] for token in sentence
                   if not PUNC_RX.match(token['pos1']) and
                   not (token['pos2'] == '数詞' and NUMBER_RX.match(token['orth']))]
        else:
            yield [token['orth'] for token in sentence]

#import pprint
def tokenize(text, features, no_punc=True, speech_mode='yes',
             features_separator=None, opening_delim=None, closing_delim=None):
    '''
    Returns a sequence of sentences comprised of whitespace separated
    tokens. Supports encoding tokens with other POS or morphological
    annotations.
    '''
    first_feature = features[0]
    rest_features = features[1:]

    # Transform tab to real tab to deal with terminal passthrough issue.
    if opening_delim == 'tab':
        opening_delim = "\t"
    if closing_delim == 'tab':
        closing_delim = "\t"

    if len(features) == 1:
        opening_delim, closing_delim = '', ''
    else:
        if not opening_delim:
            opening_delim, closing_delim = '/', ''
        if not closing_delim:
            closing_delim = ''

    if not features_separator:
        features_separator = ','
    elif features_separator == 'tab':
        features_separator = "\t"

    for sentence in text_to_tokens(text, speech_mode):
        tokens = []
        if no_punc:
            tokens = [str(token[first_feature] +
                          opening_delim +
                          features_separator.join(token[feature] for feature in rest_features) +
                          closing_delim).replace('\n', '')

                      for token in sentence
                      if not PUNC_RX.match(token['pos1']) and
                      not (token['pos2'] == '数詞' and NUMBER_RX.match(token['orth']))]
        else:
            tokens = [
                '{}{}{}{}'.format(
                    token[first_feature],
                    opening_delim,
                    features_separator.join(token[feature] for feature in rest_features),
                    closing_delim
                ).replace('\n', '')
                for token in sentence
            ]

        if tokens:
            yield tokens

def romanize(s: str) -> str:
    return re.sub(r'_+',
                  '_',
                  re.sub(r'[^a-zA-Z]',
                         '_',
                         jaconv.kana2alphabet(jaconv.kata2hira(s.replace('ゔ', 'v')))))


def read_aozora_bunko_list(path: str, ndc_tr: Dict[str, str]) -> defaultdict:
    '''
    Reads in the list_person_all_extended_utf8.csv of Aozora Bunko and
    constructs a nested dictionary keyed on author and title. This is
    then used identify the correct path to the file as well as give
    more metadata.
    '''
    d = defaultdict(dict)
    url_rx = re.compile(r'https://www\.aozora\.gr\.jp/cards/(\d+)/(.+)')
    with ZipFile(path) as z:
        with z.open('list_person_all_extended_utf8.csv', 'r') as f:
            for row in csv.DictReader(TextIOWrapper(f)):
                # Some works have versions in both new- and old-style
                # kana. As we are only interested in the new-style
                # version, we skip the old one while keeping only
                # old-style works.
                if row['文字遣い種別'] != '新字新仮名':
                    log.warning(f'Skipping processing of old-syle kana work: {row}')
                    continue

                # Use the lower value from 底本初版発行年1 and 初出:
                year = ''

                year_rx = re.compile(r'(\d{4})（.+）年\s?(\d{1,2})月((\d{1,2})日)?')

                year_matches = year_rx.match(row['底本初版発行年1'])
                if year_matches and year_matches.groups():
                    year = year_matches.groups()[0]

                year_alternate_matches = year_rx.search(row['初出'])
                if year_alternate_matches and year_alternate_matches.groups():
                    alt_year = year_alternate_matches.groups()[0]
                    if year == '':
                        year = alt_year
                    elif int(alt_year) < int(year):
                        year = alt_year

                # Sanity check for year:
                year_death = re.search(r'\d{4}', row['没年月日'])
                if year_death and year_death.groups() and int(year_death.groups()[0]) < int(year):
                    year = '<' + year_death  # Specify upper bound as last resort.

                author_ja = row['姓'] + row['名']
                author_en = row['名ローマ字'] + ' ' + row['姓ローマ字']
                title = row['作品名']
                title_ja = title
                title_en = jaconv.kana2alphabet(jaconv.kata2hira(row['作品名読み'])).title()
                subtitle = row['副題']
                if subtitle != '':
                    title_ja += ': ' + subtitle
                    title_en += ': ' + romanize(row['副題読み']).title()

                try:
                    match = url_rx.match(row['XHTML/HTMLファイルURL'])
                    id = match.group(1)
                    file_path = match.group(2)
                except AttributeError:
                    log.debug('Missing XHTML/HTML file for record {}, skipping...'.format(row))
                    continue

                ndc = row['分類番号'].replace('NDC ', '').replace('K', '')

                if len(ndc) > 3:
                    ndcs = ndc.split()
                    ndc = '/'.join(ndc_tr[n] for n in ndcs)
                elif not ndc:
                    ndc = ''
                else:
                    ndc = ndc_tr[ndc]

                if 'K' in row['分類番号']:
                    ndc += ' (児童書)'

                if title in d[author_ja]:
                    # Remove translations.
                    d[author_ja].pop(title, None)
                    if len(d[author_ja]) == 0:
                        d.pop(author_ja, None)
                else:
                    d[author_ja][title] = {
                        'author_ja': author_ja,
                        'author': author_en,
                        'author_year': f'{row["生年月日"]}--{row["没年月日"]}',
                        'title_ja': title_ja,
                        'title': title_en,
                        'year': year,
                        'ndc': ndc,
                        'file_path': 'aozorabunko/cards/{}/{}'.format(id, file_path),
                        'file_name': '{}_{}_{}'.format(  # TODO Do we need to shorthen these?
                            row['姓ローマ字'],
                            row['名ローマ字'][0:1],
                            romanize(row['作品名読み'][0:7]).title()
                        )
                    }
    return d


def read_author_title_list(
    aozora_db: defaultdict,
    path: str
) -> Tuple[List[Tuple[str, str]], List[OrderedDict]]:
    '''
    Reads in the author title table that is used to extract a subset
    of author-title pairs from Aozora Bunko. The CSV file must contain
    the columns 'author' and 'title'. Output is a list of corpus files
    and a database containing metadata.

    The reader supports an optional '*' value for the title field. If
    it encounters one, it will match on all the works of the
    author. To extract all texts from Aozora Bunko, see the `--all`
    flag.
    '''
    corpus_files = []
    db = []
    with open(path, newline='') as f:
        r = csv.DictReader(f)
        for row in r:
            if row['corpus'] == 'Aozora Bunko':
                try:
                    row['author'] = re.sub(r'\s', '', row['author'])
                    if row['title'] == '*':
                        works = {title: (m['file_name'], m['file_path'])
                                 for title, m in aozora_db[row['author']].items()}
                        corpus_files.extend((row['corpus'],) + file_name_path
                                            for file_name_path in works.values())
                        db.extend({'corpus': row['corpus'],
                                   'corpus_id': works[title][1],
                                   'author': row['author'],
                                   'title': title,
                                   'brow': '',
                                   'genre': '',
                                   'narrative_perspective': '',
                                   'comments': ''}
                                  for title in works.keys())
                    else:
                        match = aozora_db[row['author']][row['title']]
                        corpus_files.append((row['corpus'], match['file_name'], match['file_path']))
                        row['corpus_id'] = match['file_path']
                        db.append(row)
                except KeyError:
                    log.warning('{} not in Aozora Bunko DB. Skipping...'.format(row))
            else: # Corpus is not Aozora Bunko:
                file_path = join(row['corpus'], row['filename'])
                corpus_files.append((row['corpus'], splitext(row['filename'])[0], file_path))
                row['corpus_id'] = file_path
                db.append(row)

    return corpus_files, db


def remove_from(s, pattern):
    rx = re.compile(pattern, re.M)
    maybe_match = rx.search(s)
    if maybe_match:
        log.warning('Removing: {}'.format(s[maybe_match.start():]))
        return s[0:maybe_match.start()]
    else:
        return s


def read_aozora_bunko_xml(path, gaiji_tr, should_tokenize, features, no_punc, speech_mode,
                          features_separator, opening_delim, closing_delim):
    '''
    Reads an Aozora Bunko XHTML/HTML file and converts it into plain
    text. All comments and ruby are removed, and gaiji are replaced
    with Unicode equivalents.
    Reference:
    -   http://www.aozora.gr.jp/annotation/
    -   http://www.aozora.gr.jp/annotation/henkoten.html
    '''

    with open(path, 'rb') as f:
        doc = html.parse(f.read(), maybe_xhtml=False, fallback_encoding='shift_jis', return_root=False)
    body = doc.xpath(".//div[@class='main_text']")

    if len(body) == 0:
        log.warn('Error extracting main_text from file {}, trying workaround...'.format(path))
        body = doc.xpath(".//body")
        if len(body) == 0:
            log.critical('Error extracting text from file {} by any means'.format(path))
            return [[]], 0
        else:
            body = body[0]
    else:
        body = body[0]

    # Remove ruby and notes:
    for e in body.xpath("""
      .//span[@class='notes']
    | .//rp
    | .//rt
    | .//sub
    | .//div[@class='bibliographical_information']
    | .//div[@class='notation_notes']
    | .//div[@class='after_text']
    """):
        parent = e.getparent()
        assert parent is not None
        if e.tail:
            previous = e.getprevious()
            if previous is None:
                parent.text = (parent.text or '') + e.tail
            else:
                previous.tail = (previous.tail or '') + e.tail
        parent.remove(e)

    # Convert gaiji img tags to Unicode characters:
    for gaiji_el in body.xpath(".//img[@class='gaiji']"):
        menkuten = re.match(r'.+gaiji/\d+-\d+/(\d-\d+-\d+)\.png', gaiji_el.get('src')).groups(1)[0]
        gaiji_el.text = gaiji_tr[menkuten]
        log.debug('Replacing JIS X {} with Unicode \'{}\''.format(menkuten, gaiji_tr[menkuten]))

    text = re.sub(r'[\r\n]+', '\n', ''.join(body.itertext()).strip(), flags=re.MULTILINE)
    text = remove_from(text, r'^[　【]?(底本：|訳者あとがき|この翻訳は|この作品.*翻訳|この翻訳.*全訳)')

    if should_tokenize:
        paragraphs = [list(tokenize(paragraph,
                                    features,
                                    no_punc=no_punc,
                                    speech_mode=speech_mode,
                                    features_separator=features_separator,
                                    opening_delim=opening_delim,
                                    closing_delim=closing_delim))
                      for paragraph in text.splitlines()]
        token_count = sum(len(sentence)
                          for paragraph in paragraphs
                          for sentence in paragraph)
    else:
        paragraphs, token_count = None, None

    return text, paragraphs, token_count


def write_corpus_file(text, paragraphs, file_name, prefix):
    '''
    Given a sequence of paragraphs and path to output, writes plain
    and tokenized versions of the paragraphs.
    '''
    with open('{}/Plain/{}.txt'.format(prefix, file_name), 'w') as f_plain:
        f_plain.write(text)

    if paragraphs is not None:
        with open('{}/Tokenized/{}.txt'.format(prefix, file_name), 'w') as f_tokenized:
            for paragraph in paragraphs:
                f_tokenized.write('<PGB>\n'.join('\n'.join(sentence) + '\n<EOS>\n'
                                                 for sentence in paragraph))


def convert_corpus_file(corpus, file_name, file_path, prefix, gaiji_tr, should_tokenize,
                        features=['orth'], no_punc=True, speech_mode='yes', min_tokens=False,
                        features_separator=None, opening_delim=None, closing_delim=None):
    '''
    Helper function that reads in html and writes a plain/tokenized
    version in one step. Needed for concurrent.futures.
    '''
    if corpus != 'Aozora Bunko':
        with open(file_path) as f:
            text = f.read()
            paragraphs = [list(tokenize(paragraph,
                                        features,
                                        no_punc=no_punc,
                                        speech_mode=speech_mode,
                                        features_separator=features_separator,
                                        opening_delim=opening_delim,
                                        closing_delim=closing_delim))
                          for paragraph in text.splitlines()]
            token_count = sum(len(sentence)
                              for paragraph in paragraphs
                              for sentence in paragraph)
    else:
        try:
            text, paragraphs, token_count = read_aozora_bunko_xml(
                file_path,
                gaiji_tr,
                should_tokenize,
                features,
                no_punc,
                speech_mode,
                features_separator,
                opening_delim,
                closing_delim,
            )
        except UnicodeDecodeError as e:
            text, paragraphs, token_count = '', [], 0
            log.warn(f'Decoding of {file_path} failed with {e}')
    reject = min_tokens and token_count is not None and token_count < min_tokens
    if not reject:
        write_corpus_file(text, paragraphs, file_name, prefix)
    return file_name, file_path, prefix, token_count, reject


def write_metadata_file(
    files: List[Tuple[str, str]],
    metadata: List[OrderedDict],
    aozora_db: defaultdict,
    prefix: str
) -> None:
    '''
    Writes metadata of processed author-title pairs for further
    analysis.
    '''
    metadata_fn = '{}/groups.csv'.format(prefix)
    with open(metadata_fn, 'w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['filename',
                         'brow',
                         'language',
                         'corpus',
                         'corpus_id',
                         'author_ja',
                         'title_ja',
                         'author',
                         'title',
                         'author_year',
                         'year',
                         'token_count',
                         'ndc',
                         'genre',
                         'narrative_perspective',
                         'comments'])
        for (corpus, file_name, _), d in zip(files, metadata):
            if corpus != 'Aozora Bunko':
                writer.writerow([file_name + '.txt',
                                 d['brow'],
                                 'ja',
                                 corpus,
                                 d['corpus_id'],
                                 d['author_ja'],
                                 d['title_ja'],
                                 d['author'],
                                 d['title'],
                                 d['author_year'],
                                 d['year'],
                                 d['token_count'],
                                 d['ndc'],
                                 d['genre'],
                                 d['narrative_perspective'],
                                 d['comments']])
            else:
                try:
                    m = aozora_db[d['author']][d['title']]
                    writer.writerow([file_name + '.txt',
                                     d['brow'],
                                     'ja',
                                     corpus,
                                     d['corpus_id'],
                                     m['author_ja'],
                                     m['title_ja'],
                                     m['author'],
                                     m['title'],
                                     m['author_year'],
                                     m['year'],
                                     d['token_count'],
                                     m['ndc'],
                                     d['genre'],
                                     d['narrative_perspective'],
                                     d['comments']])
                except KeyError:
                    log.critical(f'Missing keys for {file_name} in "{d}" with author metadata {aozora_db[d["author"]]}')
        log.info('Wrote metadata to {}'.format(metadata_fn))
