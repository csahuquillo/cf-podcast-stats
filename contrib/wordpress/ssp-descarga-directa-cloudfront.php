<?php
/**
 * Plugin Name: SSP descarga directa desde CloudFront
 * Description: Evita que Seriously Simple Podcasting haga de proxy del MP3 en las
 *   descargas con ?ref=download. SSP solo hace proxy si la URL no fue modificada por
 *   el filtro ssp_enclosure_url; al añadir dl=1 redirige (302) a CloudFront, que así
 *   ve la IP y el país reales del oyente (antes todo salía como la IP del servidor, Francia).
 */
add_filter( 'ssp_enclosure_url', function ( $file, $episode_id, $referrer ) {
	if ( 'download' === $referrer && is_string( $file ) && false !== strpos( $file, 'podcast.sahuquillo.org' ) ) {
		return add_query_arg( 'dl', '1', $file );
	}
	return $file;
}, 10, 3 );
