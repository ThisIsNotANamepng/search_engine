# Search Engine

import tokenizer
import scraper
import sqlite3

DATABASE_PATH = "database.db"

def create_database():
    connection = sqlite3.connect("aquarium.db")
    cursor = connection.cursor()
    cursor.execute("CREATE TABLE bigrams (name TEXT, species TEXT, tank_number INTEGER)")


def search(query):

    print(tokenizer.tokenize_all(query))

def store(url):
    text = scraper.get_main_text(url)
    if text:
        print("Storing article text")
        # Here you would add code to store the text in your database
        print(text)
    else:
        print("Failed to retrieve article text.")


#query=input("Search query: ")
query = "How do I hack a website"
#search(query)

print(tokenizer.tokenize_all(scraper.get_main_text("https://www.bbc.com/news/articles/cde6yld78d6o")))