# The Database

## Redis

## Blocklist

## Web Text Storage

Some notes from development, will format this later:

Tricks for maximum efficiency

1. Send the text as the raw request body, not with json, thus no parsing and processing. Metadata (url, title) goes in the query string
2. gzip the text. Fewer bytes across the connection means less latency. Still optional though,  set `Content-Encoding: gzip` to use
3. Compress and upload as a stream of chunks so whole pages aren't loaded into memory at once

These functions are self-contained (only need `requests` and the stdlib) so
you can copy them into your scraper. They are defined but never run on import.

## Index

Mermaid diagram

``` mermaid
classDiagram
  Person <|-- Student
  Person <|-- Professor
  Person : +String name
  Person : +String phoneNumber
  Person : +String emailAddress
  Person: +purchaseParkingPass()
  Address "1" <-- "0..1" Person:lives at
  class Student{
    +int studentNumber
    +int averageMark
    +isEligibleToEnrol()
    +getSeminarsTaken()
  }
  class Professor{
    +int salary
  }
  class Address{
    +String street
    +String city
    +String state
    +int postalCode
    +String country
    -validate()
    +outputAsLabel()
  }
```

I'm going to have to pull out my sql class notes for the entity-relationship graph

``` mermaid
erDiagram
  CUSTOMER ||--o{ ORDER : places
  ORDER ||--|{ LINE-ITEM : contains
  LINE-ITEM {
    string name
    int pricePerUnit
  }
  CUSTOMER }|..|{ DELIVERY-ADDRESS : uses
```