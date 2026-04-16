from AudioBooks.Catalog.Repository.CatalogRepository import CatalogRepository
import os

def test_pagination():
    repo = CatalogRepository()
    limit = 5
    
    # Page 1
    books1 = repo.get_books(limit=limit, offset=0)
    # Page 2
    books2 = repo.get_books(limit=limit, offset=limit)
    
    print(f"Retrieved {len(books1)} books for page 1.")
    print(f"Retrieved {len(books2)} books for page 2.")
    
    if len(books1) == 0:
        print("Error: No books found in the database.")
        return

    # Check that they are different
    titles1 = [b['title'] for b in books1]
    titles2 = [b['title'] for b in books2]
    
    print(f"Page 1 first title: {titles1[0]}")
    print(f"Page 2 first title: {titles2[0]}")
    
    if titles1[0] != titles2[0]:
        print("SUCCESS: Pages are different. Pagination is working at the repository level.")
    else:
        print("FAILURE: Page 1 and Page 2 are identical. Check LIMIT/OFFSET implementation.")

if __name__ == '__main__':
    test_pagination()
