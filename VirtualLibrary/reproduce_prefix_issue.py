
import os
import sys
from VirtualLibrary.Book import Book
from VirtualLibrary.CatalogManagement import CatalogManagement
from VirtualLibrary.FuzzySearch import FuzzySearch

def test_prefix_search():
    # Setup
    fuzzy = FuzzySearch()
    # Ensure index exists and is clean for the test
    try:
        fuzzy.opensearch_client.indices.delete(index=fuzzy.opensearch_index)
    except:
        pass
    fuzzy.create_index()

    book1 = Book(title="The Hobbit", author="J.R.R. Tolkien", genre=["Fantasy"], 
                 isbn="12345", images=[], description="A big adventure.", 
                 tags=[], publication_date="1937", publisher="Allen & Unwin")
    book2 = Book(title="1984", author="George Orwell", genre=["Dystopian"], 
                 isbn="67890", images=[], description="Big Brother.", 
                 tags=[], publication_date="1949", publisher="Secker & Warburg")
    
    fuzzy.index_books([book1, book2])

    print("\nTesting Prefix Search:")
    
    # Test 1: Full title match
    results = fuzzy.search("Hobbit")
    print(f"Search 'Hobbit': {[res['title'] for res in results]}")
    
    # Test 2: Prefix match "Hob"
    results = fuzzy.search("Hob")
    print(f"Search 'Hob': {[(res['title'], res['score']) for res in results]}")

    # Test 3: Prefix match "198"
    results = fuzzy.search("198")
    print(f"Search '198': {[(res['title'], res['score']) for res in results]}")

if __name__ == "__main__":
    # Add current directory to path so it can find VirtualLibrary
    sys.path.append(os.getcwd())
    test_prefix_search()
