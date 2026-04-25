#word count is not greater than (prefix_score + bigram_score + trigram_score + word_score) -> causes relevance to skyrocket and be above 2
#check if jack hagen is actually referenced in jack's website (IN WHAT WE SCRAPED)
#make something so it checks for word strings and matches those within a website? - maybe something to thing about (consider runtime)
#make this print the number of matching bigrams, trigrams, prefixes, and number of words for any url

#make default value for relevance = 0
#make default value for references = 0, then try to do relevance + references = search_output (or if references == 0, do .75*references = references)
#add HUGE relevance boost toward websites that are like disney.com and NOT disney.com/xyz or just get rid of non primary domains

#((prefix_score + bigram_score + trigram_score + word_score + word_count) / word_count + bigram_score + trigram_score) TRY THIS

link = "https://conde-nast-es.myshopify.com/customer_authentication/?redirectlocale=es&region_country=ES/"

clean_link = link.split('?', 1)[0] #gets rid of anything after a ?
clean_link = clean_link.split('#', 1)[0] #gets rid of anything after a #

slash_index = clean_link.rfind("/") #finds where the last "/" is in a string and returns the index of that
clean_link_length = len(clean_link)-1 #returns max index value of the link
if slash_index == clean_link_length:
    clean_link = clean_link[:-1] #gets rid of the "/" if a link ends with it
print(clean_link)