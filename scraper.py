# THIS IS A SHITTY BASIC SCRAPER JUST INTENDED FOR TESTING AND DEVELOPMENT OF THE DATABASE, IT WILL BE CHANGED





import requests
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from readability import Document
from bs4 import BeautifulSoup

def allowed_by_robots(url, user_agent="*"):
    """
    Check robots.txt to see if we are allowed to fetch the URL.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        print(f"Could not read robots.txt ({robots_url}): {e}")
        return True  # If no robots.txt, crawl baby crawl

    return rp.can_fetch(user_agent, url)

def get_main_text(url):
    """
    Downloads and extracts the main readable text from an article.
    """
    # Check robots.txt
    if not allowed_by_robots(url):
        print("Access to this URL is disallowed by robots.txt.")
        return False

    # Fetch HTML
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()

    # Use readability to extract the main content
    doc = Document(resp.text)
    html = doc.summary()

    # Clean HTML -> text
    soup = BeautifulSoup(html, "html.parser")

    return soup.get_text(separator="\n").strip()

