import opensearchpy
from opensearchpy import OpenSearch
from typing import List
from VirtualLibrary.Book import Book
import time

from VirtualLibrary.settings import get_openai_api_key, get_openai_model


class FuzzySearch:

    opensearch_index: str = "books_index"
    opensearch_client: opensearchpy.OpenSearch = None

    def __init__(self, host: str = "http://localhost:9200", enable_opensearch: bool = True):
        self.pipeline_id = "book-vectorization-pipeline"
        self.model_id = None  # To be set after registration/deployment

        # Allow instantiation without OpenSearch when only LLM refinement is needed (tests, offline runs).
        self.opensearch_client = None
        if enable_opensearch:
            # Disable security locally (SSL=False, auth=None)
            self.opensearch_client = OpenSearch(host, use_ssl=False, verify_certs=False)
            self._setup_ml_model()
            self._setup_pipeline()

    def _setup_ml_model(self):
        """
        Registers and deploys the sentence-transformers model using ML Commons.
        """
        
        # 1. Check if model already registered
        search_body = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"name": "all-MiniLM-L6-v2"}},
                        {"match": {"model_format": "TORCH_SCRIPT"}}
                    ]
                }
            }
        }
        try:
            res = self.opensearch_client.transport.perform_request("GET", "/_plugins/_ml/models/_search", body=search_body)
            if res['hits']['total']['value'] > 0:
                # Pick the first one that is DEPLOYED if possible
                deployed_models = [h for h in res['hits']['hits'] if h['_source'].get('model_state') == 'DEPLOYED']
                if deployed_models:
                    self.model_id = deployed_models[0]['_id']
                    print(f"Deployed model found: {self.model_id}")
                    return
                else:
                    self.model_id = res['hits']['hits'][0]['_id']
                    print(f"Registered model found: {self.model_id}")
            else:
                # 2. Register model
                register_body = {
                    "name": "huggingface/sentence-transformers/all-MiniLM-L6-v2",
                    "version": "1.0.1",
                    "model_format": "TORCH_SCRIPT"
                }
                reg_res = self.opensearch_client.transport.perform_request("POST", "/_plugins/_ml/models/_register", body=register_body)
                task_id = reg_res['task_id']
                print(f"Registering model, task_id: {task_id}")
                
                # Wait for registration
                self.model_id = self._wait_for_task(task_id)
                print(f"Model registered successfully: {self.model_id}")

            # 3. Deploy model if not deployed
            model_info = self.opensearch_client.transport.perform_request("GET", f"/_plugins/_ml/models/{self.model_id}")
            if model_info.get('model_state') != 'DEPLOYED':
                print(f"Deploying model: {self.model_id}")
                dep_res = self.opensearch_client.transport.perform_request("POST", f"/_plugins/_ml/models/{self.model_id}/_deploy")
                task_id = dep_res['task_id']
                try:
                    self._wait_for_task(task_id)
                    print(f"Model deployed successfully: {self.model_id}")
                except Exception as de:
                    print(f"Warning: Deployment task failed (check memory): {de}")

        except Exception as e:
            print(f"Warning: ML model setup failed: {e}")
            # Fallback will be handled by self.model_id being None or existing value

    def _wait_for_task(self, task_id, timeout=60):
        start_time = time.time()
        while time.time() - start_time < timeout:
            res = self.opensearch_client.transport.perform_request("GET", f"/_plugins/_ml/tasks/{task_id}")
            if res['state'] == 'COMPLETED':
                return res.get('model_id') or res.get('target_model_id')
            if res['state'] == 'FAILED':
                raise Exception(f"Task {task_id} failed: {res.get('error')}")
            time.sleep(2)
        raise Exception(f"Timeout waiting for task {task_id}")

    def _setup_pipeline(self):
        # 2. Define the pipeline for automatic vectorization
        if not self.model_id:
            print("Warning: No model_id available for pipeline setup.")
            return

        pipeline_body = {
            "description": "Vectorize book descriptions automatically",
            "processors": [
                {
                    "text_embedding": {
                        "model_id": self.model_id, 
                        "field_map": {
                            "description": "description_vector"
                        }
                    }
                }
            ]
        }
        try:
            self.opensearch_client.ingest.put_pipeline(id=self.pipeline_id, body=pipeline_body)
            print(f"Pipeline '{self.pipeline_id}' created successfully with model {self.model_id}.")
        except Exception as e:
            print(f"Warning: Could not create pipeline: {e}")

    def create_index(self):
        mapping = {
            "settings": {
                "index": {
                    "knn": True,
                    "default_pipeline": self.pipeline_id,
                    "analysis": {
                        "filter": {
                            "library_synonyms": {
                                "type": "synonym",
                                "synonyms": [
                                    "wizard, mage, sorcerer",
                                    "hobbit, halfling",
                                    "dystopian, totalitarian",
                                    "sci-fi, science fiction"
                                ]
                            }
                        },
                        "analyzer": {
                            "library_analyzer": {
                                "tokenizer": "standard",
                                "filter": [
                                    "lowercase",
                                    "library_synonyms"
                                ]
                            }
                        }
                    }
                }
            },
            "mappings": {
                "properties": {
                    "book_id": {"type": "integer"},
                    "title": {
                        "type": "text", 
                        "analyzer": "library_analyzer", 
                        "fields": {"keyword": {"type": "keyword"}}
                    },
                    "description": {
                        "type": "text",
                        "analyzer": "library_analyzer"
                    },
                    "description_vector": {
                        "type": "knn_vector",
                        "dimension": 384,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene"
                        }
                    }
                }
            }
        }
        if not self.opensearch_client.indices.exists(index=self.opensearch_index):
            self.opensearch_client.indices.create(index=self.opensearch_index, body=mapping)
            print(f"Index '{self.opensearch_index}' created.")

    def index_books(self, books: List[Book]):
        for book in books:
            doc = {
                "book_id": book.book_id,
                "title": book.title,
                "description": book.description
            }
            try:
                self.opensearch_client.index(index=self.opensearch_index, body=doc, id=book.book_id)
            except Exception as e:
                print(f"Warning: Indexing error (likely ML model not loaded): {e}")
                # Fallback: indexing without pipeline
                try:
                    self.opensearch_client.index(index=self.opensearch_index, body=doc, id=book.book_id, pipeline="_none")
                    print(f"Indexed book {book.book_id} without ML pipeline.")
                except Exception as e2:
                    print(f"Critical indexing error: {e2}")
        self.opensearch_client.indices.refresh(index=self.opensearch_index)

    def refine_query_via_llm(self, user_query: str) -> str:
        """
        Uses an LLM (OpenAI, Claude, or a local model) to refine the user's search query.
        This handles typo correction, keyword expansion, and intent identification.
        
        To use a real LLM, you would call an external API or a local LLM client here.
        If NO API key is found, it falls back to a rule-based simulation for testing.
        """
        api_key = get_openai_api_key()
        
        if api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)
                response = client.chat.completions.create(
                    model=get_openai_model(),
                    messages=[
                        {"role": "system", "content": "You are a search query optimizer for a library. Correct typos, expand keywords, and extract core intent. Return ONLY the refined search string."},
                        {"role": "user", "content": f"Refine this search query: {user_query}"}
                    ],
                    temperature=0
                )
                refined = response.choices[0].message.content.strip()
                print(f"Refined query via OpenAI: '{user_query}' -> '{refined}'")
                return refined
            except ImportError as e:
                print(f"Warning: OpenAI python package not available: {e}")
            except Exception as e:
                print(f"Warning: OpenAI refinement failed: {e}")
        
        # Simulation removed as per user request. Fallback to original query.
        print(f"OpenAI unavailable or failed, using original query: '{user_query}'")
        return user_query

    def search(self, user_text: str, top_k: int = 5) -> List[dict]:
        # 1. Query Refinement (LLM)
        refined_query = self.refine_query_via_llm(user_text)
        
        # 2. Try Keyword match first (with fuzziness and synonyms via OpenSearch)
        keyword_query = {
            "query": {
                "bool": {
                    "should": [
                        {
                            "multi_match": {
                                "query": refined_query,
                                "fields": ["title^3", "description"],
                                "fuzziness": "AUTO",
                                "operator": "or"
                            }
                        },
                        {
                            "multi_match": {
                                "query": refined_query,
                                "fields": ["title^3"],
                                "type": "phrase_prefix"
                            }
                        }
                    ]
                }
            }
        }
        
        try:
            response = self.opensearch_client.search(index=self.opensearch_index, body=keyword_query)
            hits = response['hits']['hits']
        except Exception as e:
            print(f"Error during search: {e}")
            hits = []
        
        # 3. If no strong match, fallback/augment with semantic search
        if not hits or (hits and hits[0]['_score'] < 1.0):
            # Hybrid/Semantic Search
            hybrid_query = {
                "query": {
                    "bool": {
                        "should": [
                            {
                                "multi_match": {
                                    "query": refined_query,
                                    "fields": ["title^2", "description"],
                                    "fuzziness": "AUTO"
                                }
                            },
                            {
                                "neural": {
                                    "description_vector": {
                                        "query_text": user_text, # Original user text for context
                                        "model_id": self.model_id or "sentence-transformers__all-minilm-l6-v2",
                                        "k": top_k
                                    }
                                }
                            }
                        ]
                    }
                }
            }
            try:
                response = self.opensearch_client.search(index=self.opensearch_index, body=hybrid_query)
                hits = response['hits']['hits']
            except Exception as e:
                print(f"Error during semantic search (likely ML model not loaded): {e}")
                # Fallback to simple keyword search on description if neural fails
                fallback_query = {
                    "query": {
                        "multi_match": {
                            "query": refined_query,
                            "fields": ["title^2", "description"],
                            "fuzziness": "AUTO"
                        }
                    }
                }
                try:
                    response = self.opensearch_client.search(index=self.opensearch_index, body=fallback_query)
                    hits = response['hits']['hits']
                except Exception as e3:
                    print(f"Fallback search error: {e3}")
                    hits = []
            
        results = []
        for hit in hits:
            results.append({
                "book_id": hit['_source'].get('book_id'),
                "title": hit['_source'].get('title'),
                "score": hit['_score']
            })
            
        return results
