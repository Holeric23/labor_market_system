"""Corpus-trained context similarity and phrase clustering for review proposals."""

import csv
import json
from pathlib import Path


def propose(profiles, extractor, output, min_documents=5, max_candidates=4000, seed=42):
    import joblib
    import numpy as np
    from sklearn.cluster import MiniBatchKMeans
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    # Frequency is per unique text, not repeated mentions within a post.
    known = sorted((k,v) for k,v in profiles.items() if k.startswith('skill:'))
    unknown = sorted(((k,v) for k,v in profiles.items() if k.startswith('candidate:')
                      and v['document_count'] >= min_documents),
                     key=lambda kv:(-kv[1]['document_count'],kv[0]))[:max_candidates]
    fields = ['candidate','document_count','vacancy_count','resume_count','cluster_id',
              'suggested_skill_id','suggested_skill_name','context_similarity','spelling_similarity',
              'top_matches_json','examples_json','decision','target_skill_id','new_skill_name','new_category']
    output = Path(output)
    summary = {'eligible_candidates':len(unknown),'known_profiles':len(known),
               'min_documents':min_documents,'max_candidates':max_candidates,
               'method':'TF-IDF context profiles + LSA; char ngrams; MiniBatchKMeans',
               'automatically_accepted':0,'similarity_is_probability':False}
    with (output/'skill_proposals.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        if len(known)+len(unknown)<3 or not unknown or not known:
            summary['status']='insufficient_profiles'
            return summary
        entities=known+unknown
        documents=[]
        names=[]
        for key,profile in entities:
            name = extractor.skills[key[6:]]['name'] if key.startswith('skill:') else key[10:]
            names.append(extractor.normalizer.phrase(name))
            # Context profiles are intentionally topical; they do not prove equivalence.
            documents.append(' '.join(extractor.normalizer.phrase(e['context']) for e in profile['examples']))
        tfidf=TfidfVectorizer(ngram_range=(1,2),min_df=2,max_df=1.0,max_features=20000,sublinear_tf=True)
        try:
            matrix=tfidf.fit_transform(documents)
        except ValueError:
            summary['status']='insufficient_context_vocabulary'
            return summary
        components=min(64,matrix.shape[0]-1,matrix.shape[1]-1)
        if components<2:
            summary['status']='insufficient_context_dimensions'
            return summary
        svd=TruncatedSVD(n_components=components,random_state=seed)
        embeddings=normalize(svd.fit_transform(matrix))
        nknown=len(known)
        similarities=embeddings[nknown:] @ embeddings[:nknown].T
        char=TfidfVectorizer(analyzer='char_wb',ngram_range=(2,5),lowercase=False)
        # Compare spelling against every registered alias, not just English canonical names.
        alias_items=sorted(extractor.aliases.items())
        char_matrix=char.fit_transform([a for a,_ in alias_items]+names[nknown:])
        char_sim=(char_matrix[len(alias_items):] @ char_matrix[:len(alias_items)].T).toarray()
        clusters=min(60,max(1,int(len(unknown)**.5)))
        cluster_model=MiniBatchKMeans(n_clusters=clusters,random_state=seed,n_init=3,batch_size=512)
        assignments=cluster_model.fit_predict(embeddings[nknown:])
        known_index={key[6:]:i for i,(key,_) in enumerate(known)}
        for i,(key,profile) in enumerate(unknown):
            spelling=np.zeros(nknown)
            for j,(_,skill_id) in enumerate(alias_items):
                if skill_id in known_index:
                    idx=known_index[skill_id]
                    spelling[idx]=max(spelling[idx],char_sim[i,j])
            # Retrieval ranking only. Both component scores remain visible.
            ranking=.65*similarities[i]+.35*spelling
            top=np.argsort(-ranking)[:3]
            matches=[{'skill_id':known[j][0][6:],'context_similarity':round(float(similarities[i,j]),4),
                      'spelling_similarity':round(float(spelling[j]),4)} for j in top]
            best=matches[0]
            writer.writerow({'candidate':key[10:],'document_count':profile['document_count'],
                             'vacancy_count':profile['post_types'].get('vacancy',0),
                             'resume_count':profile['post_types'].get('resume',0),
                             'cluster_id':int(assignments[i]),'suggested_skill_id':best['skill_id'],
                             'suggested_skill_name':extractor.skills[best['skill_id']]['name'],
                             'context_similarity':best['context_similarity'],
                             'spelling_similarity':best['spelling_similarity'],
                             'top_matches_json':json.dumps(matches,ensure_ascii=False),
                             'examples_json':json.dumps(profile['examples'],ensure_ascii=False),
                             'decision':'','target_skill_id':'','new_skill_name':'','new_category':''})
        joblib.dump({'tfidf':tfidf,'svd':svd,'spelling_vectorizer':char,'clusters':cluster_model,
                     'entities':[key for key,_ in entities], 'known_aliases':alias_items,
                     'embeddings':embeddings,'seed':seed},output/'context_model.joblib')
        summary.update(status='trained',dimensions=components,clusters=clusters,
                       explained_variance=float(svd.explained_variance_ratio_.sum()))
        (output/'context_profiles.json').write_text(json.dumps(dict(entities),ensure_ascii=False),encoding='utf-8')
    return summary
