"""Bounded public article reader; no paywall bypass and no full-text archive."""
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import urlsplit
from ..common.timeutil import parse_dt

from .transport import Transport


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden=0
        self.depth=0
        self.article_depth=None
        self.all_text=[]
        self.article_text=[]
        self.title=[]
        self.in_title=False
        self.json_scripts=[]
        self.script_buffer=None

    def handle_starttag(self,tag,attrs):
        attr=dict(attrs)
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.depth+=1
        if tag=='script':
            self.hidden+=1
            if attr.get('type')=='application/ld+json':
                self.script_buffer=[]
        elif tag in ('style','nav','footer','aside'):
            self.hidden+=1
        if tag=='title': self.in_title=True
        if tag=='article' or attr.get('itemprop')=='articleBody':
            if self.article_depth is None: self.article_depth=self.depth

    def handle_endtag(self,tag):
        if tag in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            return
        if tag=='script' and self.script_buffer is not None:
            self.json_scripts.append(''.join(self.script_buffer));self.script_buffer=None
        if tag in ('script','style','nav','footer','aside'):self.hidden=max(0,self.hidden-1)
        if tag=='title':self.in_title=False
        if self.article_depth==self.depth:self.article_depth=None
        self.depth=max(0,self.depth-1)

    def handle_data(self,data):
        if self.script_buffer is not None:self.script_buffer.append(data)
        text=data.strip()
        if self.in_title:self.title.append(text)
        if not self.hidden and text and not self.in_title:
            self.all_text.append(text)
            if self.article_depth is not None:self.article_text.append(text)


def read_public_article(url,transport=None,expected_title=None):
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname in {'localhost','127.0.0.1','::1'}:
        raise ValueError('original public HTTPS article URL required')
    result=(transport or Transport(retries=0,timeout=15)).get(url,expect='text')
    if not result.ok:
        return {'status':'unavailable','url':url,'reason':result.error,'http_status':result.http_status}
    parser=ArticleParser();parser.feed(result.content.decode('utf-8',errors='replace'))
    def article_nodes(value):
        if isinstance(value,dict):
            if isinstance(value.get('articleBody'),str):yield value
            for child in value.values():
                yield from article_nodes(child)
        elif isinstance(value,list):
            for child in value:
                yield from article_nodes(child)
    embedded='';published_at=None
    nodes=[]
    for block in parser.json_scripts:
        try:
            packed=json.loads(block)
            nodes.extend(article_nodes(packed))
        except (ValueError,TypeError):pass
    def normalise(value):return re.sub(r'\W+',' ',value.lower()).strip()
    expected=normalise(expected_title or ' '.join(parser.title).split('|')[0])
    matching=[node for node in nodes if normalise(node.get('headline',node.get('name',''))) == expected]
    node=matching[0] if matching else (nodes[0] if len(nodes)==1 else None)
    if node:
        embedded=node['articleBody']
        value=node.get('datePublished')
        if isinstance(value,str) and ('Z' in value or '+' in value[10:] or '-' in value[10:]):
            try:published_at=parse_dt(value).isoformat()
            except ValueError:pass
    text=embedded or '\n'.join(parser.article_text or parser.all_text)
    if not embedded:
        # Related-story cards often use <article> while the main story uses a
        # plain div. Locate the document's own headline in visible text.
        title=' '.join(parser.title).split('|')[0].strip()
        title=re.sub(r'Arrow (?:up|down)\s*','',title).strip()
        title=re.sub(r'^X\s+','',title)
        all_text='\n'.join(parser.all_text)
        position=all_text.lower().find(expected_title.lower()) if expected_title else -1
        if position<0:position=all_text.lower().find(title.lower()) if title else -1
        if position>=0:
            text=all_text[position:]
    text=re.sub(r'Our analysts just identified a stock.*?Tap here\.', '',text,flags=re.I|re.S)
    if len(text)<300 or any(term in text[:4000].lower() for term in ('access denied','verify you are human','enable javascript and cookies','we do not host the original article','stores the headline, source, and snippet')):
        return {'status':'unavailable','url':url,'reason':'body_unavailable_or_access_restricted'}
    return {'status':'read','url':url,'title':' '.join(parser.title),'body':text,
        'content_sha256':hashlib.sha256(result.content).hexdigest(),'read_at':result.fetched_at.isoformat(),
        'published_at':published_at,
        'storage_policy':'in-memory full body; archive only brief quote, own summary, locator and hash'}
