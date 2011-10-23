<%inherit file="/base.mako" />
<%namespace name="artlib" file="/art/lib.mako" />

<%def name="title()">Artwork tagged "${tag.name}"</%def>

<section>
<h1>${title()}</h1>

${artlib.render_gallery_sieve(gallery_sieve)}
</section>
