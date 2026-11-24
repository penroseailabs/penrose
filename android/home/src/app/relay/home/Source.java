package app.relay.home;

import java.util.List;

/** One place results come from. Adds its hits for the query, each with a score; Search ranks them. */
interface Source {
    void search(Query q, List<Result> out);
}
